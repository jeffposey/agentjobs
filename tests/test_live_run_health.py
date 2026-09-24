"""A task read says what the run holding it is doing, so only real activity animates (task-570).

"Working" is derived from the record alone: an agent holds the ball on an active task.
A task whose session died still reads it, so a chip that moved on "Working" would move on
exactly the task where nothing is happening. ``live_run_health`` is the run board's own
word for the run holding the task's lock -- ``ledger.run_health``, not a new derivation --
and the React chip animates "Working" only when it says ``working`` or ``starting``.

The run directories and lock files are the shapes the ledger writes; the reader is real.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import reset_dependency_cache
from agentjobs.api.live_run import live_run_health
from agentjobs.api.main import app
from agentjobs.dispatch.ledger import KIND_FINISH, locks_root

from test_execution_controller import Machine, machine

__all__ = ["machine"]  # a fixture, imported by name -- the harness is task-416's


def a_run(
    home: Path, run_id: str, *, task_id: str, status: str, project_id: str = "sandbox"
) -> None:
    """A dispatched session's run directory and the task lock that names it."""
    directory = home / "runs" / run_id
    directory.mkdir(parents=True)
    meta: Dict[str, Any] = {
        "run_id": run_id,
        "task_id": task_id,
        "project_id": project_id,
        "mode": "session",
        "status": status,
        "session_id": "abc123",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    (directory / "meta.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")
    locks = locks_root(home)
    locks.mkdir(parents=True, exist_ok=True)
    (locks / f"{project_id}~{task_id}.lock").write_text(
        f"pid={os.getpid()} run={run_id} kind=dispatch started=2026-01-01T00:00:00+00:00",
        encoding="ascii",
    )


@pytest.fixture()
def served(machine: Machine) -> Iterator[TestClient]:
    reset_dependency_cache()
    with TestClient(app) as client:
        yield client
    reset_dependency_cache()


def listing(client: TestClient) -> Dict[str, Dict[str, Any]]:
    response = client.get("/api/projects/sandbox/tasks")
    assert response.status_code == 200, response.text
    return {row["id"]: row for row in response.json()}


class TestTheRowCarriesTheRunsHealth:
    def test_working_parked_and_no_run_are_three_answers(
        self, machine: Machine, served: TestClient
    ) -> None:
        """All three read "Working"; only the first has a run doing anything."""
        working, parked, orphan = machine.task(), machine.task(), machine.task()
        for task_id in (working, parked, orphan):
            machine.manager.claim_task(task_id, agent="claude")
        a_run(machine.home, "run_w", task_id=working, status="running")
        a_run(machine.home, "run_p", task_id=parked, status="parked")

        rows = listing(served)

        assert [rows[t]["display_status"] for t in (working, parked, orphan)] == ["Working"] * 3
        assert rows[working]["live_run_health"] == "working"
        assert rows[parked]["live_run_health"] == "parked"
        assert rows[orphan]["live_run_health"] is None

    def test_the_task_page_carries_it_too(self, machine: Machine, served: TestClient) -> None:
        task_id = machine.task()
        machine.manager.claim_task(task_id, agent="claude")
        a_run(machine.home, "run_s", task_id=task_id, status="starting")

        response = served.get(f"/api/projects/sandbox/tasks/{task_id}/detail")

        assert response.status_code == 200, response.text
        assert response.json()["task"]["live_run_health"] == "starting"


class TestWhatTheLookupIgnores:
    def test_a_concluded_run_holds_nothing(self, machine: Machine) -> None:
        """A lock whose run is over is stale, and ``live_lock_holders`` drops it."""
        a_run(machine.home, "run_done", task_id="task-001", status="finished")
        assert live_run_health(machine.home, "sandbox") == {}

    def test_another_projects_run_is_not_this_projects(self, machine: Machine) -> None:
        a_run(machine.home, "run_o", task_id="task-001", status="running", project_id="other")
        assert live_run_health(machine.home, "sandbox") == {}

    def test_a_finish_lock_names_no_run(self, machine: Machine) -> None:
        """A finish is ``live_finish``'s fact; this field never reports one."""
        locks = locks_root(machine.home)
        locks.mkdir(parents=True, exist_ok=True)
        (locks / "sandbox~task-001.lock").write_text(
            f"pid={os.getpid()} run= kind={KIND_FINISH} finish=fin_x started=2026-01-01T00:00:00+00:00",
            encoding="ascii",
        )
        assert live_run_health(machine.home, "sandbox") == {}
