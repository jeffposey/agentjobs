"""Stand up the chain panel on its own port, with throwaway data (task-150).

Two tasks, seeded so both sides of the change can be compared without constructing
either by hand:

    task-001  a chain that converged -- four turns, criteria flipping one at a time,
              ending with every check met. This is what a loop that worked looks like,
              and the grid should read as a staircase down the columns.
    task-002  a chain that is still live -- two turns in, one criterion settled and one
              not. **Revoke is clickable here**, which is the control section 9 asks
              for, and clicking it is safe: nothing on this server is real.

A third, task-003, has never had a chain, so the panel is absent. That is the
comparison that matters most: a panel appearing on every task is a panel nobody reads.

Nothing here touches the live corpus. Everything lives under a temporary directory that
is deleted when this process stops, including its own ``AGENTJOBS_HOME`` registry, so
the 8876 dashboard and its registry are not involved at all.

    python scripts/chain_panel_sandbox.py [port]

Stop it with Ctrl-C, or by killing the process.

**The chains here are seeded, not driven.** Driving a real one needs four agent runs and
twenty minutes, which is not a review. What is real is every row the panel reads: the
``chain_authorized`` entries, the revocation and the ``check_result`` vectors are all
written through the manager verbs the driver uses, so the page is reading exactly what a
real chain would have left behind.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

DEFAULT_PORT = 8913

#: Each seeded task: its title, and the result vector at each turn. ``None`` for a
#: criterion a turn did not decide.
CONVERGED = [
    ["failed", "failed", "failed"],
    ["met", "failed", "failed"],
    ["met", "met", "failed"],
    ["met", "met", "met"],
]

LIVE = [
    ["failed", "failed"],
    ["met", "failed"],
    ["met", "failed"],
]


def _outcomes(statuses: list[str], criteria: list[str]) -> list[dict[str, object]]:
    """One pass's outcomes, shaped the way the evaluator writes them."""
    return [
        {
            "id": criterion,
            "status": status,
            "exit_code": 0 if status == "met" else 1,
            "duration_seconds": 3.4,
            "output_tail": None if status == "met" else f"{criterion} still fails",
        }
        for criterion, status in zip(criteria, statuses)
    ]


def seed(manager) -> None:
    """Three tasks: a converged chain, a live one, and one that never had a chain."""
    from agentjobs.dispatch.chains import check_digest
    from agentjobs.models_v2 import CheckOutcome, Lifecycle

    def make(task_id: str, title: str, criteria: list[str], prose: bool) -> object:
        acceptance = [
            {
                "id": criterion,
                "text": f"Criterion {criterion} holds",
                "check": [sys.executable, "-c", "pass"],
            }
            for criterion in criteria
        ]
        if prose:
            acceptance.append({"id": "sc-prose", "text": "It reads well", "verify": "Look at it."})
        manager.create_task(
            id=task_id,
            title=title,
            category="general",
            summary=f"{title}.",
            description="Seeded for the chain panel review.",
            lifecycle=Lifecycle.READY,
            actor="Jeff Posey",
            acceptance=acceptance,
        )
        stored = manager.get_task(task_id)
        assert stored is not None
        return stored

    def write_chain(
        task_id: str,
        criteria: list[str],
        vectors: list[list[str]],
        *,
        chain_id: str,
        revoke: bool,
        hours: int,
    ) -> None:
        task = manager.get_task(task_id)
        manager.record_chain_authorization(
            task_id,
            actor="Jeff Posey",
            chain_id=chain_id,
            max_iterations=5,
            wall_clock_seconds=hours * 3600,
            check_digest=check_digest(task),
            criteria=criteria,
            body=(
                f"Jeff Posey authorised chain `{chain_id}` against {task_id}: at most 5 "
                f"iterations within {hours}h."
            ),
        )
        for iteration, statuses in enumerate(vectors):
            results = [CheckOutcome.model_validate(item) for item in _outcomes(statuses, criteria)]
            passed = sum(1 for status in statuses if status == "met")
            manager.record_check_result(
                task_id,
                actor="dispatcher",
                results=results,
                unchecked=["sc-prose"],
                chain_id=chain_id,
                iteration=iteration,
                body=(
                    f"Iteration {iteration} of 5"
                    + (" (baseline at authorisation)" if iteration == 0 else "")
                    + f": {passed} of {len(statuses)} checks pass."
                ),
            )
        if revoke:
            manager.record_chain_revocation(
                task_id,
                actor="dispatcher",
                chain_id=chain_id,
                body=(
                    f"Chain `{chain_id}` is spent: it converged at iteration "
                    f"{len(vectors) - 1} and the ball is with a human."
                ),
            )

    make("task-001", "A chain that converged", ["sc-1", "sc-2", "sc-3"], prose=True)
    write_chain(
        "task-001",
        ["sc-1", "sc-2", "sc-3"],
        CONVERGED,
        chain_id="chain_c04e6ged",
        revoke=True,
        hours=4,
    )

    make("task-002", "A chain still running", ["sc-1", "sc-2"], prose=True)
    write_chain(
        "task-002",
        ["sc-1", "sc-2"],
        LIVE,
        chain_id="chain_11e0a11e",
        revoke=False,
        hours=12,
    )

    make("task-003", "A task with no chain at all", ["sc-1"], prose=True)


def build(root: Path, *, project_id: str) -> Path:
    """A registered project holding the three seeded tasks."""
    from agentjobs.manager import TaskManager
    from agentjobs.project_setup import build_project_config
    from sandbox_store import sandbox_store  # type: ignore[import-not-found]

    project_root = root / project_id
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(
            build_project_config(project_name="Sandbox: agent loops", user="Jeff Posey"),
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (project_root / ".gitignore").write_text(".agentjobs/\ntasks/\n", encoding="utf-8")
    seed(TaskManager(sandbox_store(project_root / "tasks", project_id=project_id)))
    return project_root


def write_dispatch_config(home: Path, root: Path, project_id: str) -> None:
    """A machine that permits the sandbox project, so Revoke is a live button.

    ``require_clean_tree`` is off: this sandbox writes task records on every click, and a
    gate that shuts the moment somebody presses a button is not one to review behind.
    """
    fake = root / "fake-agent.py"
    fake.write_text("import sys\nprint('sandbox agent started:', sys.argv[1:])\n", encoding="utf-8")
    config = {
        "version": 1,
        "enabled": True,
        "runners": {
            "sandbox": {"argv": [sys.executable, str(fake), "{prompt}"], "actor": "claude"}
        },
        "projects": {
            project_id: {"enabled": True, "runner": "sandbox", "require_clean_tree": False}
        },
    }
    (home / "dispatch.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def serve(port: int) -> None:
    root = Path(tempfile.mkdtemp(prefix="agentjobs-chain-panel-"))
    atexit.register(shutil.rmtree, root, True)
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)
    sys.path.insert(0, str(Path(__file__).resolve().parent))

    project_id = "sandbox-chains"
    from agentjobs.projects import ProjectRegistry

    project_root = build(root, project_id=project_id)
    write_dispatch_config(home, root, project_id)
    ProjectRegistry(home).add(project_root, project_id=project_id, name="Sandbox: agent loops")

    from sandbox_serve import serve  # type: ignore[import-not-found]

    from agentjobs.api.main import app

    base = f"http://127.0.0.1:{port}/app/p/{project_id}"
    print(f"[review] chain panel sandbox at http://127.0.0.1:{port}/app/", flush=True)
    print(f"[review]   converged chain: {base}/tasks/task-001", flush=True)
    print(f"[review]   live chain, Revoke is clickable: {base}/tasks/task-002", flush=True)
    print(f"[review]   no chain, so no panel: {base}/tasks/task-003", flush=True)
    print(f"[review] throwaway data under {root}", flush=True)
    serve(app, port=port)


def main() -> None:
    port = DEFAULT_PORT
    for argument in sys.argv[1:]:
        if argument.isdigit():
            port = int(argument)
    serve(port)


if __name__ == "__main__":
    main()
