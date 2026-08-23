"""Stand up the dispatch UI on a machine with runner groups, and on one without.

task-184 changed what one page says about a configuration it could already see. The
whole point is that a project pointed at a ``group:`` used to render "no runner chosen"
beside an open gate, and that a dispatch could not name a group -- neither of which is
visible in a diff, and both of which need a configured machine to reproduce.

Runner groups are defined **per machine**, not per project, so the two states this
change affects cannot both exist behind one server. This script therefore starts two,
on adjacent ports, and stops both together:

    port      ~/.agentjobs/dispatch.yaml       projects
    --------  ------------------------------  ---------------------------------------
    8900      two runners, two groups         grouped: ``group: default``
                                              runner:  a plain ``runner:``, on a
                                                       machine that has groups
    8901      two runners, no groups at all   flat:    what every machine looked like
                                                       before groups existed

The second server is the control. Nothing on it should differ by a character from what
it showed before task-177 -- no Group pulldown, bare runner names in the enable select,
and the same sentence beside the Dispatch button.

Everything here is throwaway. Press anything, including Disable and Dispatch: the
configs, the registries and the tasks all live under temporary directories these
processes delete when they stop, and the "agent" a dispatch starts is a Python script
that prints one line and exits.

    python scripts/dispatch_groups_sandbox.py [port]

Stop it with Ctrl-C, which stops both. The ports default to ones of their own so they
cannot be confused with the real dashboard: a second server on the usual port silently
serves stale code from a process nobody restarts.

What to look for, since "it renders" is not the property under review:

  * On **grouped**, the Dispatch page's THIS PROJECT tile reads
    ``group: default -> claude-opus-5``. It used to read "no runner chosen" while every
    gate was open, which is the defect this task was filed for.
  * On its seeded task, a **Group** pulldown sits beside the Dispatch button. Its first
    option spells out what the project would resolve to; the rest are this machine's
    groups. Pick ``big`` and the sentence beside it stops naming a member and names the
    group instead -- the browser holds no member list, so guessing which member wins
    would be the one sentence there that is reliably wrong.
  * Press Dispatch with ``big`` picked, then read the task log's dispatch entry: it
    records the group the run was chosen from, which is the choice having reached the
    API rather than the page having drawn a control.
  * Press **Disable dispatch**, then look at the enable control. It lists this machine's
    groups *and* its runners, preselected on ``group: default`` -- what the project
    actually uses. Before this it preselected the first runner, and pressing Enable then
    changed nothing at all, silently: the config layer keeps a project's group and
    declines a runner sent beside it. Re-enable against ``group: big``; the tile follows.
  * On **runner** (same machine, plain ``runner:``), the tile reads ``runner: <name>``
    and the Group pulldown is still offered -- a group named on one dispatch outranks
    the project's runner, and that is a real thing to be able to ask for.
  * On **flat**, port 8901, none of that appears anywhere: no Group pulldown, the enable
    select is labelled "Runner" over bare names, and the tile reads ``runner: <name>``.
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

DEFAULT_PORT = 8900

#: Two names for one harmless script. Group selection asks only whether ``argv[0]`` is
#: on PATH, so this interpreter satisfies it and every member of every group is
#: genuinely eligible -- which makes ``default -> claude-opus-5`` a resolution rather
#: than a caption.
RUNNERS = ("claude-opus-5", "claude-sonnet-5")

GROUPED = "grouped"
RUNNER = "runner"
FLAT = "flat"

NAMES = {
    GROUPED: "Sandbox: pointed at a group",
    RUNNER: "Sandbox: a plain runner, on a machine with groups",
    FLAT: "Sandbox: a machine with no groups at all",
}


def seed(manager, *, title: str) -> str:
    """One ready task whose newest entry is a human's, so Dispatch is offered on it.

    A dispatch may only follow a human's log entry, so a task an agent filed and never
    touched renders the refusal rather than the button -- and the button is the thing
    being reviewed.
    """
    from agentjobs.models_v2 import Lifecycle, LogEntryType, Priority

    task = manager.create_task(
        title=title,
        category="general",
        summary=f"{title}.",
        description=(
            "Seeded so the dispatch controls can be looked at against this machine's "
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


def write_dispatch_config(home: Path, root: Path, *, with_groups: bool) -> None:
    """This machine's dispatch.yaml, with or without any notion of a group."""
    fake = root / "fake-agent.py"
    fake.write_text("import sys\nprint('sandbox agent started:', sys.argv[1:])\n", encoding="utf-8")
    argv = [sys.executable, str(fake), "{prompt}"]
    projects: dict[str, dict[str, object]] = {}
    config: dict[str, object] = {
        "version": 1,
        "enabled": True,
        "runners": {name: {"argv": list(argv), "actor": "claude"} for name in RUNNERS},
        "projects": projects,
    }
    if with_groups:
        config["runner_groups"] = {
            "default": {
                "description": "Cheapest capable model first.",
                "members": list(RUNNERS),
            },
            "big": {
                "description": "The big model, for work worth paying for.",
                "members": list(reversed(RUNNERS)),
            },
        }
        projects[GROUPED] = {"enabled": True, "group": "default", "require_clean_tree": False}
        projects[RUNNER] = {"enabled": True, "runner": RUNNERS[1], "require_clean_tree": False}
    else:
        projects[FLAT] = {"enabled": True, "runner": RUNNERS[1], "require_clean_tree": False}
    (home / "dispatch.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def serve(port: int, *, with_groups: bool) -> None:
    """One machine, one server, on its own throwaway home."""
    root = Path(tempfile.mkdtemp(prefix="agentjobs-dispatch-groups-"))
    atexit.register(shutil.rmtree, root, True)
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    registry = ProjectRegistry(home)
    label = "with groups" if with_groups else "no groups at all"
    for project_id in (GROUPED, RUNNER) if with_groups else (FLAT,):
        project_root, task_id = build(root, project_id=project_id)
        registry.add(project_root, project_id=project_id, name=NAMES[project_id])
        base = f"http://127.0.0.1:{port}/app/p/{project_id}"
        print(f"[review]   {project_id:<8} {base}/dispatch", flush=True)
        print(f"[review]   {'':<8} {base}/tasks/{task_id}", flush=True)

    write_dispatch_config(home, root, with_groups=with_groups)
    print(f"[review] machine on {port}: {label}; data under {root}", flush=True)

    import uvicorn

    from agentjobs.api.main import app

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def main() -> None:
    args = [value for value in sys.argv[1:] if value != "--flat"]
    port = int(args[0]) if args else DEFAULT_PORT

    if "--flat" in sys.argv[1:]:
        serve(port, with_groups=False)
        return

    # The control machine is a second process, because a runner group is defined per
    # machine and one process reads one AGENTJOBS_HOME. Started first so its URLs are
    # printed before this one blocks on uvicorn.
    flat = subprocess.Popen([sys.executable, __file__, str(port + 1), "--flat"])
    atexit.register(flat.terminate)
    print(f"[review] dispatch-groups sandbox: {port} has groups, {port + 1} has none", flush=True)
    print("[review] stop with Ctrl-C; both stop, and the data goes with them.", flush=True)
    try:
        serve(port, with_groups=True)
    finally:
        flat.terminate()


if __name__ == "__main__":
    main()
