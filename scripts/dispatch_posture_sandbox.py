"""Stand up the posture pulldown against every configuration it behaves differently in.

task-307 added a control beside the Dispatch button that chooses what one run may do.
What it offers, what each option says, and whether it appears at all are decided by
machine-local settings a diff cannot show and the live dashboard has only one of. So
this seeds all four states at once and lets them be compared by switching project.

Unlike the runner-group sandbox next door, everything here is **per project**, so one
server on one port is enough:

    project     max_posture   finish   push    what the page should show
    ----------  ------------  -------  ------  ---------------------------------------
    raised      autonomous    on       off     the full pulldown, every option live
    nofinish    autonomous    off      off     the same list, but `autonomous` greyed
                                               out and saying why
    unraised    unset         on       off     a pulldown that can only *narrow*: no
                                               `autonomous` in it at all
    readonly    read_only     on       off     no pulldown at all
    pushes      autonomous    on       ON      the full pulldown, and the sentence
                                               beside it naming that this project
                                               publishes

**`unraised` is the one to check first**, because it is what every real project looks
like today, including agentjobs itself. It is also where the obvious guess is wrong, and
this sandbox exists partly because the first draft of these notes got it wrong: an unset
ceiling does *not* remove the control. `max_posture` unset means the ceiling is the
project's own posture, so a project at `auto` still offers `read_only`, `supervised` and
`auto`. What it cannot do is escalate -- which is the double opt-in the ceiling is for.

`readonly` is the only configuration where the control vanishes, because `read_only` is
the narrowest posture there is and so the only ceiling with nothing underneath it.

Everything here is throwaway. Press anything, including Dispatch: the configs, the
registries and the tasks live under a temporary directory this process deletes when it
stops, and the "agent" a dispatch starts is a script that prints one line and exits.

    python scripts/dispatch_posture_sandbox.py [port]

Stop it with Ctrl-C, and the data goes with it. The port defaults to one of its own so
it cannot be confused with the real dashboard -- a second server on the usual port
silently serves stale code from a process nobody restarts.

What to look for, since "it renders" is not the property under review:

  * On **raised**, the seeded task shows an **Envelope** pulldown beside Dispatch. Every
    option names what it does to the *branch*, not what it is called: `autonomous —
    merges its own work when the gate passes, no review`. That sentence is the whole
    point. "autonomous" alone tells a reader nothing about whether their work merges
    without them, and since task-021 that is exactly what it decides.
  * Pick `autonomous` and watch the sentence beside the pulldown follow it -- it must
    stop describing the project's default and describe what this click will actually do.
  * Press Dispatch with it picked, then read the task log's dispatch entry: it records
    `posture: autonomous` and `posture_source: dispatch`. That is the choice having
    reached the API, rather than the page having drawn a control.
  * On **nofinish**, `autonomous` is listed but **cannot be picked**, and says
    `needs finish.enabled on this project`. This is the case worth arguing about, so the
    reasoning is here rather than only in the task record: task-021 decided an autonomous
    merge runs through `agentjobs finish --posture-release`, and a machine without that
    switched on has no sanctioned mechanism for one. Offering it anyway would start a run
    told in its prompt that it may merge, with no way to do it -- which is how an agent
    talks itself into an improvised `git merge`. Hiding it instead would leave the reader
    wondering why the option they were promised is missing, when the fix is one line of
    their own config.
  * On **unraised**, the pulldown is there and `autonomous` is simply not in it. This is
    the shape every real project has, and the one to look hardest at: escalation costs a
    hand edit to a machine-local file, and this is what the control looks like until
    somebody makes it.
  * On **readonly**, there is no Envelope pulldown anywhere, and the sentence beside the
    button reads as it always did.
  * On **pushes**, the sentence beside the button includes `and pushes`. Push is per
    project and never the posture's (task-021), it is false everywhere real, and it is
    the half that a revert cannot undo -- which is why it is said out loud here and
    stays silent on the other three.
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

DEFAULT_PORT = 8902

#: One harmless script wearing a name that looks like a real runner, so the resolution
#: printed beside the button is a resolution rather than a caption.
RUNNER = "claude-opus-5"

RAISED = "raised"
NOFINISH = "nofinish"
UNRAISED = "unraised"
READONLY = "readonly"
PUSHES = "pushes"

NAMES = {
    RAISED: "Sandbox: ceiling raised, finish on",
    NOFINISH: "Sandbox: ceiling raised, no scripted finish",
    UNRAISED: "Sandbox: no ceiling raised, so narrowing only",
    READONLY: "Sandbox: capped at read_only, so nothing to choose",
    PUSHES: "Sandbox: ceiling raised, and this one pushes",
}

#: Each project's entry in ``dispatch.yaml``. The differences here are the whole fixture.
#: Everything not named here is the same across all five, so anything that differs on
#: screen differs because of one of these lines.
SETTINGS: dict[str, dict[str, object]] = {
    RAISED: {"max_posture": "autonomous", "finish": {"enabled": True}},
    NOFINISH: {"max_posture": "autonomous"},
    UNRAISED: {"finish": {"enabled": True}},
    READONLY: {"posture": "read_only", "finish": {"enabled": True}},
    PUSHES: {"max_posture": "autonomous", "finish": {"enabled": True}, "push": True},
}


def seed(manager, *, title: str) -> str:
    """One ready task whose newest entry is a human's, so Dispatch is offered on it.

    A dispatch may only follow a human's log entry, so a task an agent filed and never
    touched renders the refusal rather than the button -- and the button is what carries
    the control being reviewed.
    """
    from agentjobs.models_v2 import Lifecycle, LogEntryType, Priority

    task = manager.create_task(
        title=title,
        category="general",
        summary=f"{title}.",
        description=(
            "Seeded so the posture pulldown can be looked at against this project's "
            "configuration. Nothing here is real work, and every control on it is safe "
            "to press -- the runner is a script that prints one line and exits."
        ),
        priority=Priority.HIGH,
        lifecycle=Lifecycle.READY,
        actor="claude",
    )
    manager.add_log_entry(
        task.id,
        actor="Jeff Posey",
        type=LogEntryType.NOTE,
        body="Go ahead and start an agent on this.",
    )
    return str(task.id)


def build(root: Path, *, project_id: str) -> tuple[Path, str]:
    """A registered project with a clean git tree and one dispatchable task."""
    from agentjobs.manager import TaskManager
    from agentjobs.project_setup import build_project_config
    from agentjobs.storage import TaskStorage

    name = NAMES[project_id]
    project_root = root / project_id
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(build_project_config(project_name=name, user="Jeff Posey"), sort_keys=False),
        encoding="utf-8",
    )
    # Tasks are ignored rather than committed: this sandbox writes to them on every
    # click, and a clean-tree gate that shuts the moment you press a button is not one.
    (project_root / ".gitignore").write_text(".agentjobs/\ntasks/\n", encoding="utf-8")
    task_id = seed(TaskManager(TaskStorage(project_root / "tasks")), title=f"Dispatch me ({name})")

    for command in (
        ["git", "init"],
        ["git", "config", "user.email", "sandbox@example.invalid"],
        ["git", "config", "user.name", "Sandbox"],
        ["git", "add", ".gitignore"],
        ["git", "commit", "-m", "sandbox"],
    ):
        subprocess.run(command, cwd=project_root, capture_output=True, check=True)
    return project_root, task_id


def write_dispatch_config(home: Path, root: Path) -> None:
    """One machine, four projects, differing only in the fields under review."""
    fake = root / "fake-agent.py"
    fake.write_text("import sys\nprint('sandbox agent started:', sys.argv[1:])\n", encoding="utf-8")
    argv = [sys.executable, str(fake), "{prompt}"]
    projects: dict[str, dict[str, object]] = {}
    for project_id, extra in SETTINGS.items():
        entry: dict[str, object] = {
            "enabled": True,
            "runner": RUNNER,
            "posture": "auto",
            "require_clean_tree": False,
        }
        entry.update(extra)
        projects[project_id] = entry
    config = {
        "version": 1,
        "enabled": True,
        "runners": {RUNNER: {"argv": argv, "actor": "claude"}},
        "projects": projects,
    }
    (home / "dispatch.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def serve(port: int) -> None:
    root = Path(tempfile.mkdtemp(prefix="agentjobs-dispatch-posture-"))
    atexit.register(shutil.rmtree, root, True)
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    registry = ProjectRegistry(home)
    print(f"[review] posture sandbox on {port}; data under {root}", flush=True)
    print("[review] stop with Ctrl-C, and the data goes with it.", flush=True)
    for project_id in SETTINGS:
        project_root, task_id = build(root, project_id=project_id)
        registry.add(project_root, project_id=project_id, name=NAMES[project_id])
        print(
            f"[review]   {project_id:<9} "
            f"http://127.0.0.1:{port}/app/p/{project_id}/tasks/{task_id}",
            flush=True,
        )

    write_dispatch_config(home, root)

    import uvicorn

    from agentjobs.api.main import app

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def main() -> None:
    serve(int(sys.argv[1]) if sys.argv[1:] else DEFAULT_PORT)


if __name__ == "__main__":
    main()
