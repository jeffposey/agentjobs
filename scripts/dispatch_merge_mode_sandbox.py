"""Stand up the merge mode chooser against every configuration it behaves differently in.

task-602 collapsed the four postures into the one choice a person makes at dispatch:
`review` (hands off for your review) or `automerge` (merges itself on a green gate).
What the chooser offers, what each option says, and whether it appears at all are
decided by machine-local settings a diff cannot show, so this seeds every state at once
and lets them be compared by switching project:

    project      dispatch.yaml says                   what the task page should show
    -----------  -----------------------------------  ---------------------------------
    allowed      merge_mode: review                   a "Merge mode" pulldown with
                 allow_automerge: true, finish on     both options live
    nofinish     allow_automerge: true, finish off    the same pulldown, `Automerge`
                                                      greyed out and saying why
    reviewonly   merge_mode: review (nothing else)    no pulldown at all; the sentence
                                                      beside Dispatch says review
    legacy       posture: auto                        exactly what `allowed` shows --
                 max_posture: autonomous              the pre-task-602 spelling still
                                                      reads, and `auto` is review

**`legacy` is the one that matters most.** Every machine's `dispatch.yaml` written
before this change says `posture: auto`, meaning *stop for review*. It must read as
`review`, never as `automerge`.

The Dashboard's slot board also gets two live runs a couple of seconds after start:
one `review`, and one `automerge` whose run directory is written in the **old** shape
(`posture: autonomous`), so the card proves a legacy run directory reads too.

Everything here is throwaway. Press anything, including Dispatch: the configs, the
registries and the tasks live under a temporary directory this process deletes when it
stops, and the "agent" a dispatch starts is a script that prints one line and exits.

    python scripts/dispatch_merge_mode_sandbox.py [port]

Stop it with Ctrl-C, and the data goes with it. The port defaults to one of its own so
it cannot be confused with the real dashboard.

What to look for, since "it renders" is not the property under review:

  * On **allowed**, the task shows a **Merge mode** pulldown beside Dispatch reading
    `Project default (review)`, `Review — hands off for your review` and
    `Automerge — merges itself on a green gate`. The sentence beside it reads
    `merge mode review: hands off for your review`; pick Automerge and it follows.
  * Press Dispatch with Automerge picked, then read the task's dispatch entry: it
    records `merge_mode: automerge` and `merge_mode_source: dispatch`.
  * On **nofinish**, Automerge is listed but cannot be picked, and says
    `needs finish.enabled on this project`.
  * On **reviewonly**, there is no pulldown, and the sentence says review.
  * On **legacy**, everything matches `allowed`.
  * On the **Dashboard**, the two run cards read `Hands off for your review` and
    `Merges itself on a green gate` -- phrases, never the bare values.
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

import yaml

DEFAULT_PORT = 8902

#: One harmless script wearing a name that looks like a real runner, so the resolution
#: printed beside the button is a resolution rather than a caption.
RUNNER = "claude-opus-5-5"

ALLOWED = "allowed"
NOFINISH = "nofinish"
REVIEWONLY = "reviewonly"
LEGACY = "legacy"

NAMES = {
    ALLOWED: "Sandbox: automerge allowed, finish on",
    NOFINISH: "Sandbox: automerge allowed, no scripted finish",
    REVIEWONLY: "Sandbox: review only, so nothing to choose",
    LEGACY: "Sandbox: an old dispatch.yaml (posture: auto)",
}

#: Each project's entry in ``dispatch.yaml``. The differences here are the whole fixture.
SETTINGS: Dict[str, Dict[str, object]] = {
    ALLOWED: {"merge_mode": "review", "allow_automerge": True, "finish": {"enabled": True}},
    NOFINISH: {"merge_mode": "review", "allow_automerge": True},
    REVIEWONLY: {"merge_mode": "review", "finish": {"enabled": True}},
    LEGACY: {"posture": "auto", "max_posture": "autonomous", "finish": {"enabled": True}},
}


def seed(manager, *, title: str) -> str:
    """One ready task whose newest entry is a human's, so Dispatch is offered on it."""
    from agentjobs.models_v2 import Lifecycle, LogEntryType, Priority

    task = manager.create_task(
        title=title,
        category="general",
        summary=f"{title}.",
        description=(
            "Seeded so the merge mode chooser can be looked at against this project's "
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
    from sandbox_store import sandbox_store  # type: ignore[import-not-found]

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
    task_id = seed(
        TaskManager(sandbox_store(project_root / "tasks", project_id=project_id)),
        title=f"Dispatch me ({name})",
    )

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
    projects: Dict[str, Dict[str, object]] = {}
    for project_id, extra in SETTINGS.items():
        entry: Dict[str, object] = {
            "enabled": True,
            "runner": RUNNER,
            "require_clean_tree": False,
        }
        entry.update(extra)
        projects[project_id] = entry
    config = {
        "version": 1,
        "enabled": True,
        "runners": {RUNNER: {"argv": argv, "actor": "claude"}},
        "limits": {"max_concurrent_runs": 3},
        "projects": projects,
    }
    (home / "dispatch.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def _ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def seed_run(home: Path, *, run_id: str, **meta: Any) -> None:
    """One run directory, in the shape ``ledger.read_run`` reads."""
    directory = home / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    body: Dict[str, Any] = {"run_id": run_id, "started_at": _ago(180), **meta}
    (directory / "meta.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")


def seed_runs(home: Path, tasks: Dict[str, str]) -> None:
    """Two live runs, deferred until the server has reconciled the ledger at boot.

    A session run the server has never heard of is concluded when it boots, so one
    seeded before that is `interrupted` before the first page loads.
    """
    seed_run(
        home,
        run_id="run_review01",
        task_id=tasks[ALLOWED],
        project_id=ALLOWED,
        mode="session",
        merge_mode="review",
        status="running",
        session_id="review01",
    )
    # Written the way a run directory from before task-602 is: `posture`, and the old
    # value. It must read as automerge, which is what `autonomous` meant.
    seed_run(
        home,
        run_id="run_legacy01",
        task_id=tasks[LEGACY],
        project_id=LEGACY,
        mode="session",
        posture="autonomous",
        status="running",
        session_id="legacy01",
        started_at=_ago(900),
    )
    print("[review] seeded a review run and a legacy automerge run", flush=True)


def serve(port: int) -> None:
    root = Path(tempfile.mkdtemp(prefix="agentjobs-dispatch-merge-mode-"))
    atexit.register(shutil.rmtree, root, True)
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)

    from agentjobs.projects import ProjectRegistry

    registry = ProjectRegistry(home)
    print(f"[review] merge mode sandbox on {port}; data under {root}", flush=True)
    print("[review] stop with Ctrl-C, and the data goes with it.", flush=True)
    print(f"[review]   dashboard  http://127.0.0.1:{port}/app/", flush=True)
    tasks: Dict[str, str] = {}
    for project_id in SETTINGS:
        project_root, task_id = build(root, project_id=project_id)
        tasks[project_id] = task_id
        registry.add(project_root, project_id=project_id, name=NAMES[project_id])
        print(
            f"[review]   {project_id:<10} "
            f"http://127.0.0.1:{port}/app/p/{project_id}/tasks/{task_id}",
            flush=True,
        )

    write_dispatch_config(home, root)
    threading.Timer(2.0, seed_runs, args=(home, tasks)).start()

    from sandbox_serve import serve  # type: ignore[import-not-found]

    from agentjobs.api.main import app

    serve(app, port=port)


def main() -> None:
    serve(int(sys.argv[1]) if sys.argv[1:] else DEFAULT_PORT)


if __name__ == "__main__":
    main()
