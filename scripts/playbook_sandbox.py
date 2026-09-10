"""Stand up the Playbooks page, on its own port, with a runner that spends nothing.

task-215 added a page whose only interesting control **starts an agent**. That makes it
awkward to review the ordinary way: the reviewer either presses nothing and approves a
screen they have not used, or presses Run on their real machine and pays for a model
call to look at a button. So this seeds a whole throwaway world instead:

    sandbox-playbooks       dispatch ON, pointed at a fake runner that prints and exits
    sandbox-playbooks-off   dispatch OFF, plus a file that will not load

Press anything. Run every playbook, twice. Nothing here touches the live corpus, the
8876 dashboard, or its registry: the projects, the tasks, the machine-local dispatch
config and the "runner" all live under a temporary directory this process deletes when
it stops, and the runner is a five-line Python script.

    python scripts/playbook_sandbox.py [port]

Stop it with Ctrl-C. It defaults to a port of its own, because a second server on the
usual port silently serves stale code from a process nobody restarts.

What to look for, since "it renders" is not the property under review:

  * **On the enabled project**, `groom` and `reorder` have a bare Run button and
    `flesh-out` has a task field beside one. That difference is the playbook's
    `target`, and guessing a target rather than asking would dispatch an agent at a
    task nobody named. `flesh-out`'s button stays disabled until you type `task-101`.
  * **Press Run on `groom`.** It creates a run task and links to it. Open that task:
    its `spec.description` is a *pointer* to `playbooks/groom.md` and its dispatch log
    entry carries `playbook` and `playbook_hash`. The brief itself appears in neither,
    and it must not -- a copy forks the first time somebody improves the playbook.
  * **Press Run on `groom` again.** A second run task, with its own number. Runs
    accumulate on purpose: they are the audit trail.
  * **On the disabled project** there is no Run button anywhere, the gate is named in
    words with what to do about it, and `watch.md` is reported as a file that will not
    load rather than quietly omitted. `watch.md` declares `kind: reactive` -- a
    category the design withdrew, which is why it does not load.
  * The Run button never appears when nobody is signed in. To see that, empty
    `actors:` in the enabled project's `.agentjobs/config.yaml` (the path is printed
    below) and reload.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

DEFAULT_PORT = 8895

FAKE_RUNNER = '''"""A stand-in for a real agent: prints what it was given and exits.

It starts no model and spends nothing, which is the entire reason the sandbox can
invite you to press Run as often as you like.
"""

import sys

print("FAKE RUNNER STARTED")
for index, argument in enumerate(sys.argv[1:], start=1):
    print(f"argv[{index}]: {argument}")
'''

BROKEN_PLAYBOOK = """---
name: watch
kind: reactive
description: A withdrawn category, kept here so the listing has something to complain about.
target: project
difficulty: hard
---

# Watch

This file does not load, and that is the point. `kind` is not a playbook field: the
reactive category was withdrawn by decision P8 of the playbooks design, and the
frontmatter contract refuses keys it does not know. A playbook that cannot be read
cannot be run, on any surface.
"""


def build(root: Path, *, project_id: str, name: str, thin_task: bool) -> Path:
    """One project with the shipped reference playbooks copied into it."""
    from agentjobs.manager import TaskManager
    from agentjobs.models_v2 import Lifecycle, Priority
    from agentjobs.playbooks import install_references
    from agentjobs.project_setup import build_project_config
    from sandbox_store import sandbox_store  # type: ignore[import-not-found]

    project_root = root / project_id
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(build_project_config(project_name=name, user="Jeff Posey"), sort_keys=False),
        encoding="utf-8",
    )
    install_references(project_root / "playbooks")
    if thin_task:
        # Something for `flesh-out` to be aimed at. Its newest entry is a human's,
        # which is what the human-clocked rule reads on a task-target run.
        TaskManager(sandbox_store(project_root / "tasks")).create_task(
            id="task-101",
            title="Thin: the notification service",
            summary="A title and a sentence, which is not enough to work from.",
            description="Somebody should write this properly.",
            priority=Priority.MEDIUM,
            lifecycle=Lifecycle.READY,
            actor="Jeff Posey",
        )
    return project_root


def write_dispatch_config(home: Path, runner: Path, *, enabled_project: str, port: int) -> Path:
    """A machine-local dispatch config that permits exactly one throwaway project.

    ``api_base`` names this sandbox's own server so the reachability gate is satisfied
    by something real, and so a dispatched "agent" is told an address that is not the
    live dashboard's.
    """
    config = {
        "version": 1,
        "enabled": True,
        "api_base": f"http://127.0.0.1:{port}",
        "runners": {
            "fake": {
                "argv": [Path(sys.executable).as_posix(), runner.as_posix(), "{prompt}"],
                "actor": "claude",
            }
        },
        "projects": {
            enabled_project: {"enabled": True, "runner": "fake", "require_clean_tree": False}
        },
    }
    path = home / "dispatch.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    root = Path(tempfile.mkdtemp(prefix="agentjobs-playbooks-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    runner = root / "fake_runner.py"
    runner.write_text(FAKE_RUNNER, encoding="utf-8")

    enabled_id, off_id = "sandbox-playbooks", "sandbox-playbooks-off"
    registry = ProjectRegistry(home)
    enabled_root = build(root, project_id=enabled_id, name="Sandbox: playbooks", thin_task=True)
    registry.add(enabled_root, project_id=enabled_id, name="Sandbox: playbooks")
    off_root = build(root, project_id=off_id, name="Sandbox: dispatch off", thin_task=False)
    (off_root / "playbooks" / "watch.md").write_text(BROKEN_PLAYBOOK, encoding="utf-8")
    registry.add(off_root, project_id=off_id, name="Sandbox: dispatch off")

    config_path = write_dispatch_config(home, runner, enabled_project=enabled_id, port=port)

    import uvicorn

    from agentjobs.api.main import app

    print(f"[review] playbook sandbox at http://127.0.0.1:{port}/app/", flush=True)
    print(
        f"[review]   dispatch ON   http://127.0.0.1:{port}/app/p/{enabled_id}/playbooks", flush=True
    )
    print(f"[review]   dispatch OFF  http://127.0.0.1:{port}/app/p/{off_id}/playbooks", flush=True)
    print("[review] the runner prints and exits; pressing Run spends nothing.", flush=True)
    print(f"[review] throwaway data under {root} (dispatch config: {config_path})", flush=True)
    print("[review] stop with Ctrl-C; the data is deleted with the process.", flush=True)
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
