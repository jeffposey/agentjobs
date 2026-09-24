"""Stand up the start-an-agent-on-create checkbox on its own port, with throwaway data.

task-176 puts one checkbox on the create form and on the issue reporter: unchecked by
default, and checked it starts an agent on the task in the same gesture as filing it.
Three of its four states are states nobody can construct by hand -- you cannot make a
gate shut to order, and you certainly cannot make a dispatch fail after a create
succeeds -- so this seeds each of them on one server and hands over a URL.

    python scripts/dispatch_on_create_sandbox.py [port]

**Three projects, one server. Switch between them with the project picker.**

    sandbox-open     every gate open. The box is offered; check it, file, and a real
                     (sleeping) run starts. The confirmation says an agent is working
                     on it and names the run; the link opens the task, where the run is
                     in the dispatch panel.

    sandbox-refused  every gate open too, so the box is offered and pressable -- and
                     the dispatch is then refused, because this project requires a
                     clean tree and its working tree is dirty. **This is the state the
                     whole feature turns on**: the task is filed, no run started, and
                     the confirmation says exactly that, quoting the gate and still
                     linking to the task. Nothing is rolled back -- go and look at the
                     task, it is there.

    sandbox-off      dispatch is not enabled for this project. The box is present and
                     disabled, naming the gate that is stopping it, rather than being
                     silently absent or pressable into a refusal.

**What to try, beyond looking at it.**

    * On any of the three, open Create and leave the box alone. It starts unchecked,
      every time, on every visit -- it is not remembered and never defaults on.
    * On `sandbox-open`, watch the box while you switch the starting state between
      Draft and Ready. A draft closes it, and says why: filing a draft says a person
      still has to decide the task is worth doing.
    * Press *Report issue* (bottom right, on any page) and do the same thing there.
      *Ready for an agent* is that form's lifecycle, so it governs the box the same way.
    * File with the box checked on `sandbox-open`, then open the task from the
      confirmation. The run is in the dispatch panel, attributed to Jeff Posey, and the
      authorising entry is his.
    * File with the box checked on `sandbox-refused`, twice. Both tasks exist.

Nothing here touches the live corpus or the 8876 dashboard. Everything lives under a
temporary directory with its own ``AGENTJOBS_HOME`` registry, deleted when this process
stops. Stop it with Ctrl-C.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

import yaml

DEFAULT_PORT = 8912


def build_project(root: Path, *, project_id: str, name: str, git: bool, dirty: bool) -> Path:
    """One throwaway project with a couple of tasks already in it.

    ``git`` and ``dirty`` exist for `sandbox-refused` alone: the clean-tree gate reads a
    real repository, so the only honest way to stage "the create worked and the dispatch
    did not" is to give one project a real repository with a real uncommitted file in it.
    """
    from agentjobs.manager import TaskManager
    from agentjobs.models_v2 import Lifecycle
    from agentjobs.project_setup import build_project_config
    from sandbox_store import sandbox_store  # type: ignore[import-not-found]

    project_root = root / project_id
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(build_project_config(project_name=name, user="Jeff Posey"), sort_keys=False),
        encoding="utf-8",
    )
    manager = TaskManager(sandbox_store(project_root / "tasks", project_id=project_id))
    manager.create_task(
        id="task-001",
        title="Something already in the backlog",
        summary="Seeded so the list is not empty and the project has somewhere to go back to.",
        description="Nothing to do. Seeded by scripts/dispatch_on_create_sandbox.py.",
        lifecycle=Lifecycle.READY,
        actor="Jeff Posey",
    )
    if git:
        (project_root / "README.md").write_text("A sandbox project.\n", encoding="utf-8")
        (project_root / ".gitignore").write_text(".agentjobs/\ntasks/\n", encoding="utf-8")
        for command in (
            ["git", "init"],
            ["git", "config", "user.email", "sandbox@example.invalid"],
            ["git", "config", "user.name", "Sandbox"],
            ["git", "add", "-A"],
            ["git", "commit", "-m", "seed"],
        ):
            subprocess.run(command, cwd=project_root, capture_output=True, check=False)
        if dirty:
            # The whole point of this project: a real uncommitted file, so the
            # clean-tree gate refuses the dispatch after the create has already landed.
            (project_root / "half-finished.py").write_text("# mid-edit\n", encoding="utf-8")
    return project_root


def dispatch_config(port: int) -> Dict[str, Any]:
    """A machine that will admit to one runner, with two of three projects switched on.

    The runner sleeps rather than exiting, so a dispatch started from the create form
    becomes a run that is still going when the confirmation names it.
    """
    return {
        "version": 1,
        "enabled": True,
        # This sandbox is not on the default port, so it says where it serves -- exactly
        # as a real deployment does. Without it a started run would tell its agent to
        # look at :8765 and be refused by the reachability gate, which is the right
        # refusal and not the thing this fixture is for.
        "api_base": f"http://127.0.0.1:{port}",
        "runners": {
            "sandbox-sleeper": {
                "argv": [sys.executable, "-c", "import time; time.sleep(600)"],
                "actor": "claude",
            },
        },
        "limits": {"max_concurrent_runs": 4},
        "projects": {
            "sandbox-open": {
                "enabled": True,
                "require_clean_tree": False,
                "runner": "sandbox-sleeper",
            },
            "sandbox-refused": {
                "enabled": True,
                # Every gate the *state* endpoint reports is open, so the box is offered
                # and pressable. This one is judged at the dispatch itself, which is how
                # a create can succeed and a start fail in the same click.
                "require_clean_tree": True,
                "runner": "sandbox-sleeper",
            },
            "sandbox-off": {"enabled": False, "runner": "sandbox-sleeper"},
        },
    }


def main() -> None:
    argv = [item for item in sys.argv[1:] if not item.startswith("--")]
    port = int(argv[0]) if argv else DEFAULT_PORT

    root = Path(tempfile.mkdtemp(prefix="agentjobs-dispatch-on-create-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    registry = ProjectRegistry(home)
    for project_id, name, git, dirty in (
        ("sandbox-open", "Every gate open", False, False),
        ("sandbox-refused", "Refused after the create", True, True),
        ("sandbox-off", "Dispatch switched off", False, False),
    ):
        project_root = build_project(root, project_id=project_id, name=name, git=git, dirty=dirty)
        registry.add(project_root, project_id=project_id, name=name)
    (home / "dispatch.yaml").write_text(
        yaml.safe_dump(dispatch_config(port), sort_keys=False), encoding="utf-8"
    )

    from sandbox_serve import serve  # type: ignore[import-not-found]

    from agentjobs.api.main import app

    base = f"http://127.0.0.1:{port}/app/p"
    print("[create] start-an-agent-on-create sandbox (task-176)", flush=True)
    print(f"[create]   Box offered, start works   {base}/sandbox-open/tasks/new", flush=True)
    print(f"[create]   Filed but NOT started      {base}/sandbox-refused/tasks/new", flush=True)
    print(f"[create]   Box closed, gate named     {base}/sandbox-off/tasks/new", flush=True)
    print("[create]   Report issue is the button at the bottom right of every page.")
    print("[create]   Mark the task Ready: a draft closes the box on purpose.", flush=True)
    try:
        serve(app, port=port)
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
