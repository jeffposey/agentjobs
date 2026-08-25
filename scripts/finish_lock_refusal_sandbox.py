"""task-298, sc-5: what a user actually sees when they click Dispatch during a finish.

Builds a throwaway clone, a branch in its own worktree, an open task pointing at it, and
a machine-local dispatch config with `finish.enabled`. Then spawns a REAL detached
`agentjobs finish` -- the same `finish_task` an approval spawns -- whose gate is a
`scripts/check.py` that sleeps, so the finish sits in the middle of a merge the way the
two on 2026-08-23 did. While it is in there, the parent goes through the REAL dispatch
guard (`dispatch_task`, the function the browser's POST calls) and prints the refusal
verbatim.

Nothing here touches the agentjobs repository, its home, or its server: the clone, the
worktree, the task records, the dispatch config and the run lock are all under a fresh
temp directory, and the finish is killed on the way out.

    poetry run python scripts/finish_lock_refusal_sandbox.py

Run it with a *different* checkout's interpreter to see what that checkout says about the
same situation -- which is how the before/after on task-298 was taken.
"""

import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

from agentjobs.dispatch.guards import DispatchRefused, DispatchRequest, dispatch_task
from agentjobs.dispatch.ledger import locks_root
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Lifecycle
from agentjobs.projects import Project
from agentjobs.storage import TaskStorage

SLOW_GATE = "import time\nprint('gate running')\ntime.sleep(180)\n"

CHILD = """
import sys
from pathlib import Path
import agentjobs.dispatch.finish as F
from agentjobs.dispatch.config import FinishSettings
from agentjobs.manager import TaskManager
from agentjobs.projects import Project
from agentjobs.storage import TaskStorage

root = Path(sys.argv[1]); home = Path(sys.argv[2]); task_id = sys.argv[3]
# A temp repository has no Poetry project, so the interpreter is supplied. This is the
# same substitution tests/test_dispatch_finish.py makes, and for the same reason.
F.worktree_interpreter = lambda path: Path(sys.executable)
result = F.finish_task(
    manager=TaskManager(TaskStorage(root / "tasks")),
    project=Project(id="sandbox", name="Sandbox", root=root),
    task_id=task_id,
    approver="Jeff Posey",
    home=home,
    api_base="http://127.0.0.1:1",
    settings=FinishSettings(enabled=True, base_branch="main", gate_timeout_seconds=300,
                            verify_timeout_seconds=2),
)
print(result.outcome, result.reason)
"""


def git(root: Path, *args: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="task298-"))
    root = tmp / "clone"
    (root / "tasks").mkdir(parents=True)
    (root / "scripts").mkdir(parents=True)
    git(tmp, "init", "--initial-branch=main", str(root))
    git(root, "config", "user.email", "t@t.t")
    git(root, "config", "user.name", "t")
    (root / "scripts" / "check.py").write_text(SLOW_GATE, encoding="utf-8")
    git(root, "add", "--", "scripts/check.py")
    git(root, "commit", "-m", "init")

    branch = "feat/task-001-thing"
    worktree = tmp / "worktrees" / "wt"
    git(root, "worktree", "add", "-b", branch, str(worktree), "main")
    (worktree / "docs").mkdir(parents=True, exist_ok=True)
    (worktree / "docs" / "feature.md").write_text("the deliverable\n", encoding="utf-8")
    git(worktree, "add", "--", "docs/feature.md")
    git(worktree, "commit", "-m", "docs: the deliverable")

    (root / ".agentjobs").mkdir(exist_ok=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "project_name": "Sandbox",
                "tasks_directory": "tasks",
                "actors": [
                    {"name": "Jeff Posey", "kind": "human"},
                    {"name": "claude", "kind": "agent"},
                ],
                "default_user": "Jeff Posey",
            }
        ),
        encoding="utf-8",
    )

    manager = TaskManager(TaskStorage(root / "tasks"))
    task = manager.create_task(
        title="The deliverable",
        category="infrastructure",
        summary="A task with a branch waiting to be merged.",
        description="Do the thing.",
        lifecycle=Lifecycle.READY,
        actor="Jeff Posey",
    )
    manager.claim_task(task.id, agent="claude")
    manager.update_task(task.id, actor="claude", branches=[{"name": branch, "status": "active"}])
    git(root, "add", "--", "tasks")
    git(root, "commit", "-m", "chore(tasks): the record")

    home = tmp / "home"
    home.mkdir()
    runner = tmp / "runner.py"
    runner.write_text("print('started')\n", encoding="utf-8")
    (home / "dispatch.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "enabled": True,
                "runners": {
                    "fake": {"argv": [sys.executable, str(runner), "{prompt}"], "actor": "claude"}
                },
                "projects": {
                    "sandbox": {
                        "enabled": True,
                        "runner": "fake",
                        "require_clean_tree": False,
                        "finish": {"enabled": True},
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    project = Project(id="sandbox", name="Sandbox", root=root)
    script = tmp / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    child = subprocess.Popen(
        [sys.executable, str(script), str(root), str(home), task.id],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    lock = locks_root(home) / f"{task.id}.lock"
    deadline = time.time() + 60
    while time.time() < deadline:
        # Just the file: code from before task-298 writes no finish id into it, and this
        # script has to be runnable against both to produce a before/after.
        if lock.is_file():
            break
        if child.poll() is not None:
            print("the finish exited before it took the lock:")
            print(child.communicate()[0])
            return 1
        time.sleep(0.2)
    else:
        child.kill()
        print("timed out waiting for the finish to reach the gate")
        return 1

    # Let it get properly into the gate, so the elapsed reads like a real merge rather
    # than like the moment after a click.
    time.sleep(8)
    print("lock file:", lock.read_text(encoding="utf-8"))
    print("finish pid alive:", child.poll() is None)
    print()

    try:
        dispatch_task(
            manager=manager,
            project=project,
            project_config=project.load_config(),
            request=DispatchRequest(
                task_id=task.id, authorized_by="Jeff Posey", surface="the task page"
            ),
            home=home,
            # Non-None means "the caller observed its own address", which is the browser
            # case -- it skips the loopback probe, exactly as a real POST from the task
            # page does.
            api_base="http://127.0.0.1:8876",
        )
        print("NOT REFUSED -- that is a bug in this reproduction, not a pass")
    except DispatchRefused as exc:
        print("=" * 78)
        print(f"{type(exc).__name__} (reason={exc.reason}) -- rendered verbatim on the task page:")
        print("=" * 78)
        print(str(exc))
        print("=" * 78)

    child.kill()
    child.wait(timeout=30)
    return 0


if __name__ == "__main__":
    sys.exit(main())
