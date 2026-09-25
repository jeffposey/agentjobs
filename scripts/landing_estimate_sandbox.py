"""Stand up the landing estimate on its own port, with throwaway data (task-586).

A task in the Landing state now shows a progress bar and "about N min left" on every
surface that draws it, and the estimator grades itself on the analytics page. This
serves both, from real records, so every state can be looked at without waiting for a
real merge.

    python scripts/landing_estimate_sandbox.py [port]

What to look at:

  * **The task list** of `Sandbox: landing` (`?status=all`): three Landing rows.
      - task-001 **mid-gate**: a thin bar a little under halfway and "~N min left".
      - task-002 **on the runway**: the bar pulses with no fill and reads "waiting for
        the merge runway" -- no time is invented for a lock nobody can time.
      - task-003 **running long**: the bar stopped where it got to, in amber, reading
        "taking longer than usual" rather than a negative number.
  * **Each task page**: the finish panel has a wide bar above the step table and
    "Estimate: about N min left (typical: M min)." Hover the bar for what the estimate
    rests on, including the correction it carries.
  * **The dashboard's slot board and the Runs tab**: the same three bars on the finish
    cards, and they agree with the rows because one server computation feeds them all.
  * **`Sandbox: new project`**: task-001 is Landing in a project with no finish history,
    so its row reads "Ns so far, no estimate yet" and its page says why.
  * **Analytics** of `Sandbox: landing`, the "Finishes and gates" panel, "How good the
    landing estimate was": six weeks of landings replayed through the real checkpoint
    path. The correction factor starts at none, settles near ×1.2 -- the gate's stages
    are right-skewed, so the sum of their medians under-predicts a landing, which the
    per-step medians cannot see and the correction can -- then jumps when the gate gets
    slower in the fourth week and relaxes as the medians catch up. The solid line (the
    estimate shown) sits under the dashed one (the medians alone). The reset button
    works; it deletes nothing.

Everything is the real server: the history rows and every recorded prediction are
written through the store's own methods in time order, each prediction made from only
the history that existed at that moment. The live landings are finish directories and
locks in the shapes ``dispatch.finish`` leaves, held by this process. Nothing touches
the live corpus or the 8876 dashboard: everything lives under a temporary
``AGENTJOBS_HOME``, deleted when this process stops. Stop it with Ctrl-C, or stop the
process.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

DEFAULT_PORT = 8964
PROJECT_ID = "sandbox-landing"
PROJECT_NAME = "Sandbox: landing"
NEW_PROJECT_ID = "sandbox-landing-new"
NEW_PROJECT_NAME = "Sandbox: new project"
USER = "Jeff Posey"

STEPS: Dict[str, float] = {
    "preflight": 1.2,
    "runway": 0.0,
    "rebase": 0.4,
    "gate": 0.0,  # the stages below, plus the gate's own overhead
    "catch_up": 0.0,
    "merge": 0.4,
    "rebuild": 6.0,
    "restart": 8.0,
    "verify": 0.2,
    "close": 2.2,
    "teardown": 1.0,
    "worktree": 3.8,
    "branch": 0.4,
}

STAGES: List[Tuple[str, float, float]] = [
    # name, typical seconds, the extra an unlucky run pays (right skew)
    ("black", 1.3, 0.5),
    ("ruff", 0.1, 0.1),
    ("mypy", 18.0, 25.0),
    ("api", 3.6, 1.0),
    ("icons", 1.3, 0.2),
    ("oxlint", 0.5, 0.2),
    ("pytest", 205.0, 110.0),
    ("vitest", 24.0, 18.0),
    ("build", 7.3, 3.0),
    ("e2e", 60.0, 45.0),
]

NOW = datetime.now(timezone.utc)


def stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def ago(seconds: float) -> datetime:
    return NOW - timedelta(seconds=seconds)


# ----- the replayed history ----------------------------------------------------------


def replay(store: Any, manager: Any, rng: random.Random) -> int:
    """Six weeks of landings, written as the finisher and the gate write them."""
    task = manager.create_task(
        title="History: the landings this project has already made",
        summary="The owner of the replayed finish history.",
        description="Every replayed landing belongs to this task.",
    )
    moment = NOW - timedelta(days=41)
    count = 0
    while moment < NOW - timedelta(hours=3):
        week = (moment - (NOW - timedelta(days=41))).days // 7
        slower = 170.0 if week >= 3 else 0.0  # the gate gets slower in the fourth week
        waited = 240.0 if rng.random() < 0.06 else 0.0
        retried = rng.random() < 0.05
        landing(store, task.id, f"fin_hist{count:04d}", moment, rng, slower, waited, retried)
        count += 1
        moment += timedelta(hours=rng.uniform(6, 30))
    return count


def landing(
    store: Any,
    task_id: str,
    finish_id: str,
    start: datetime,
    rng: random.Random,
    slower: float,
    waited: float,
    retried: bool,
) -> None:
    steps: List[Dict[str, Any]] = []

    def write(at: datetime, outcome: str = "running", **extra: Any) -> None:
        store.record_finish(
            finish_id,
            {"task_id": task_id, "started_at": stamp(start), "outcome": outcome, **extra},
            steps,
        )
        if outcome == "running":
            store.record_finish_checkpoint(finish_id, at)

    at = start
    write(at)
    for seq, name in enumerate(STEPS, start=1):
        if name == "gate":
            at = gate(store, finish_id, at, rng, slower, second=False)
            if retried:
                at = gate(store, finish_id, at, rng, slower, second=True)
        else:
            base = STEPS[name] + (waited if name == "runway" else 0.0)
            at += timedelta(seconds=base * rng.uniform(0.8, 1.3))
        steps.append(
            {"seq": seq, "step": name, "ok": True, "skipped": name == "catch_up", "ts": stamp(at)}
        )
        if seq < len(STEPS):
            write(at)
    write(at, "finished", finished_at=stamp(at), seconds=(at - start).total_seconds(), merged=True)


def gate(
    store: Any, finish_id: str, start: datetime, rng: random.Random, slower: float, *, second: bool
) -> datetime:
    """One full gate inside a landing, written stage by stage; returns when it ended."""
    gate_id = f"{finish_id}:g{2 if second else 1}"
    record: Dict[str, Any] = {
        "origin": "finish",
        "finish_id": finish_id,
        "scope": "full",
        "started_at": stamp(start),
    }
    stages: List[Dict[str, Any]] = []
    at = start + timedelta(seconds=1.5)
    store.record_gate_run(gate_id, record, stages)
    store.record_finish_checkpoint(finish_id, at)
    for seq, (name, typical, extra) in enumerate(STAGES, start=1):
        seconds = typical * rng.uniform(0.9, 1.1)
        if rng.random() < 0.45:
            seconds += extra * rng.uniform(0.6, 1.4)
        if name == "pytest":
            seconds += slower
        began = at
        at += timedelta(seconds=seconds)
        stages.append(
            {
                "seq": seq,
                "stage": name,
                "seconds": round(seconds, 1),
                "passed": True,
                "started_at": stamp(began),
                "finished_at": stamp(at),
            }
        )
        store.record_gate_run(gate_id, record, stages)
        if seq < len(STAGES):
            store.record_finish_checkpoint(finish_id, at)
    ended = at + timedelta(seconds=1.0)
    store.record_gate_run(
        gate_id,
        {
            **record,
            "finished_at": stamp(ended),
            "seconds": (ended - start).total_seconds(),
            "passed": True,
            "stages_run": len(STAGES),
            "stages_total": len(STAGES),
        },
        stages,
    )
    return ended


# ----- the live landings -------------------------------------------------------------


def live(
    home: Path,
    project_id: str,
    task_id: str,
    finish_id: str,
    started: float,
    lines: List[Tuple[float, Dict[str, Any]]],
) -> None:
    """A running finish on disk, and its lock held by this process."""
    from agentjobs.dispatch.ledger import locks_root

    directory = home / "finishes" / finish_id
    directory.mkdir(parents=True)
    meta = {
        "finish_id": finish_id,
        "task_id": task_id,
        "project_id": project_id,
        "outcome": "running",
        "started_at": stamp(ago(started)),
    }
    (directory / "meta.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")
    preflight = {
        "kind": "finish_preflight",
        "branch": f"feat/{task_id}-sandbox",
        "worktree": str(home),
    }
    with (directory / "phases.jsonl").open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": stamp(ago(started)), **preflight}) + "\n")
        for seconds_ago, line in lines:
            handle.write(json.dumps({"ts": stamp(ago(seconds_ago)), **line}) + "\n")
    locks = locks_root(home)
    locks.mkdir(parents=True, exist_ok=True)
    (locks / f"{project_id}~{task_id}.lock").write_text(
        f"pid={os.getpid()} run=run_sandbox kind=finish finish={finish_id} started={stamp(ago(started))}",
        encoding="utf-8",
    )


def step(name: str) -> Dict[str, Any]:
    return {"kind": "finish_step", "step": name, "ok": True, "seconds": 1.0}


def into_gate(done: List[str], stage: str, *, at: float) -> List[Tuple[float, Dict[str, Any]]]:
    """Gate records: started, each done stage finished, and ``stage`` running since ``at``."""
    lines: List[Tuple[float, Dict[str, Any]]] = [
        (at + 60, {"kind": "gate_started", "stages_total": len(STAGES)})
    ]
    for index, name in enumerate(done, start=1):
        lines.append(
            (
                at + 1,
                {
                    "kind": "gate_stage_finished",
                    "stage": name,
                    "index": index,
                    "total": len(STAGES),
                },
            )
        )
    lines.append((at, {"kind": "gate_stage_started", "stage": stage, "total": len(STAGES)}))
    return lines


def seed_tasks(manager: Any, titles: List[str]) -> None:
    from agentjobs.models_v2 import Lifecycle

    for title in titles:
        task = manager.create_task(
            title=title,
            summary=title,
            description="A sandbox task whose branch is landing.",
            lifecycle=Lifecycle.READY,
        )
        manager.claim_task(task.id, agent="claude")


def project(root: Path, home: Path, project_id: str, name: str) -> Any:
    from agentjobs.manager import TaskManager
    from agentjobs.project_setup import build_project_config
    from sandbox_store import sandbox_store  # type: ignore[import-not-found]

    project_root = root / project_id
    (project_root / ".agentjobs").mkdir(parents=True)
    (project_root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(build_project_config(project_name=name, user=USER), sort_keys=False),
        encoding="utf-8",
    )
    store = sandbox_store(project_root / "tasks", project_id=project_id)
    return store, TaskManager(store), project_root


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    root = Path(tempfile.mkdtemp(prefix="agentjobs-landing-estimate-"))
    home = root / "home"
    home.mkdir()
    os.environ["AGENTJOBS_HOME"] = str(home)
    sys.path.insert(0, str(Path(__file__).parent))

    from agentjobs.projects import ProjectRegistry

    store, manager, project_root = project(root, home, PROJECT_ID, PROJECT_NAME)
    seed_tasks(
        manager,
        [
            "Landing, mid-gate: a bar and about N min left",
            "Landing, waiting for the merge runway: indeterminate",
            "Landing, running past its estimate: taking longer than usual",
        ],
    )
    replayed = replay(store, manager, random.Random(586))
    # The analytics window never starts before a project's history baseline, and these
    # tasks were created a moment ago. The replayed landings are six weeks old, so the
    # baseline says the history goes back that far -- which, for them, it does.
    with store.database.write() as connection:
        connection.execute(
            "UPDATE project SET history_baseline_at = ?, history_baseline_kind = 'backfilled' "
            "WHERE project_id = ?",
            (stamp(NOW - timedelta(days=42)), PROJECT_ID),
        )
    ProjectRegistry(home).add(project_root, project_id=PROJECT_ID, name=PROJECT_NAME)

    _new_store, new_manager, new_root = project(root, home, NEW_PROJECT_ID, NEW_PROJECT_NAME)
    seed_tasks(new_manager, ["Landing in a project with no finish history: elapsed only"])
    ProjectRegistry(home).add(new_root, project_id=NEW_PROJECT_ID, name=NEW_PROJECT_NAME)

    early = [step("preflight"), step("runway"), step("rebase")]
    live(
        home,
        PROJECT_ID,
        "task-001",
        "fin_live_mid",
        150,
        [(146, early[0]), (145, early[1]), (144, early[2])]
        + into_gate(["black", "ruff", "mypy", "api", "icons", "oxlint"], "pytest", at=80),
    )
    live(home, PROJECT_ID, "task-002", "fin_live_wait", 95, [(93, step("preflight"))])
    live(
        home,
        PROJECT_ID,
        "task-003",
        "fin_live_long",
        1500,
        [(1496, early[0]), (1495, early[1]), (1494, early[2])]
        + into_gate(["black", "ruff", "mypy", "api", "icons", "oxlint"], "pytest", at=1300),
    )
    live(
        home,
        NEW_PROJECT_ID,
        "task-001",
        "fin_live_new",
        200,
        [(196, early[0]), (195, early[1]), (194, early[2])]
        + into_gate(["black", "ruff", "mypy"], "api", at=150),
    )

    from sandbox_serve import serve  # type: ignore[import-not-found]

    from agentjobs.api.main import app

    base = f"http://127.0.0.1:{port}/app/p"
    print(f"[landing] landing estimate sandbox at http://127.0.0.1:{port}/app/", flush=True)
    print(f"[landing]   {replayed} replayed landings behind {PROJECT_ID}", flush=True)
    print(f"[landing]   the Landing rows   {base}/{PROJECT_ID}/tasks?status=all", flush=True)
    print(f"[landing]   mid-gate page      {base}/{PROJECT_ID}/tasks/task-001", flush=True)
    print(f"[landing]   the dashboard      {base}/{PROJECT_ID}", flush=True)
    print(f"[landing]   the Runs tab       {base}/{PROJECT_ID}/runs", flush=True)
    print(f"[landing]   no history         {base}/{NEW_PROJECT_ID}/tasks?status=all", flush=True)
    print(f"[landing]   accuracy chart     {base}/{PROJECT_ID}/analytics", flush=True)
    # Lifespan off, as the other finish sandboxes do: the poller would reap the seeded
    # finishes, and those sitting where they were put is the exhibit.
    try:
        serve(app, port=port, lifespan="off")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
