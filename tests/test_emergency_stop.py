"""The emergency stop ends everything AgentJobs started, not only its live runs (task-573).

``TestStopEverything`` in ``test_dispatch_lifecycle.py`` covers the live runs and the
sentinel ordering. This file covers the three things that would start runs again the
moment the sentinel was lifted -- a pull arming, a queued dispatch and a hosted epic walk
-- and the HTTP surface the header's Stop control calls.

Each kind is seeded through production code on task-416's ``Machine`` harness, and the
"after Resume" half of each test asks the real controller tick or walk pass whether
anything starts. That is the claim: a stop that merely *paused* these would pass every
assertion about the moment of the stop and fail only here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, List

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.authorization import ROUTE_CAPABILITIES
from agentjobs.api.dependencies import reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.capabilities import Capability
from agentjobs.dispatch import pull as dispatch_pull
from agentjobs.dispatch import queue as dispatch_queue
from agentjobs.dispatch.config import clear_sentinel, sentinel_active, sentinel_path
from agentjobs.dispatch.credentials import mint_run_credential
from agentjobs.dispatch.epic import WalkStop
from agentjobs.dispatch.journal import journal
from agentjobs.dispatch.ledger import DispatchLedger, StopResult
from agentjobs.dispatch.runner import RunDirectory, new_run_id
from agentjobs.execution.store import BOUND_STARTS
from agentjobs.models_v2 import Ball, BallReason
from agentjobs.principals import RUN_CREDENTIAL_HEADER
from agentjobs.projects import HOME_ENV

from test_dispatch_pull import arm, detach, enqueue, started_tasks, walk_pass
from test_epic_supervision import Epic
from test_execution_controller import Machine, machine

__all__ = ["machine"]  # a fixture, imported by name -- the harness is task-416's


def ledger(box: Machine) -> DispatchLedger:
    """The ledger the stop runs on, with process stopping replaced.

    Only the process kill is faked, and only because a test must never reach a real
    session manager. Everything the stop decides -- what to disarm, drain and close, and
    in what order -- is production code.
    """
    stopper = DispatchLedger(box.home, managers={"sandbox": box.manager})

    def stop(record: Any) -> StopResult:
        return StopResult(record.run_id, True, "stopped (test)")

    stopper._stop = stop  # type: ignore[method-assign,unused-ignore]
    return stopper


def kinds(results: List[StopResult]) -> List[str]:
    return [result.kind for result in results]


class TestPullMode:
    def test_the_stop_disarms_it_so_resume_does_not_start_pulling(self, machine: Machine) -> None:
        machine.configure()
        machine.task()
        arm(machine, bound_kind=BOUND_STARTS, bound_starts=3)

        results = ledger(machine).stop_everything(requester="Jeff Posey")

        assert kinds(results) == ["pull"]
        assert dispatch_pull.armings(machine.home) == []
        clear_sentinel(machine.home)
        machine.tick()
        assert started_tasks(machine) == []


class TestTheQueue:
    def test_the_stop_cancels_a_waiting_dispatch_so_resume_does_not_start_it(
        self, machine: Machine
    ) -> None:
        machine.configure(limits={"max_concurrent_runs": 1})
        holding = machine.task()
        machine.dispatch(holding)
        queued = machine.task()
        enqueue(machine, queued)
        assert len(dispatch_queue.waiting(machine.home)) == 1

        results = ledger(machine).stop_everything(requester="Jeff Posey")

        assert sorted(kinds(results)) == ["queue", "run"]
        assert dispatch_queue.waiting(machine.home) == []
        task = machine.manager.get_task(queued)
        assert task is not None
        notes = [entry.body or "" for entry in task.log]
        assert any("emergency stop" in body for body in notes), notes
        clear_sentinel(machine.home)
        machine.tick()
        assert started_tasks(machine) == [holding]


class TestEpicWalks:
    def test_the_stop_closes_a_hosted_walk_and_puts_the_parent_in_front_of_a_person(
        self, machine: Machine
    ) -> None:
        machine.configure()
        epic = Epic(machine)
        epic.child("First")
        walk_id = detach(epic)

        results = ledger(machine).stop_everything(requester="Jeff Posey")

        assert kinds(results) == ["walk"]
        walk = journal(machine.home).walk(walk_id)
        assert walk is not None
        assert (walk.state, walk.stop) == ("stopped", WalkStop.EMERGENCY_STOP.value)
        parent = machine.manager.get_task(epic.parent_id)
        assert parent is not None
        assert (parent.ball, parent.ball_reason) == (Ball.HUMAN, BallReason.DECISION)
        assert "emergency stop" in (parent.ball_prompt or "")

        clear_sentinel(machine.home)
        walk_pass(epic)
        machine.tick()
        assert started_tasks(machine) == []


class TestTheSentinelSaysWhoStoppedIt:
    def test_it_names_the_requester_and_where_they_pressed_it(self, machine: Machine) -> None:
        machine.configure()

        ledger(machine).stop_everything(requester="Jeff Posey", source="the web UI")

        text = sentinel_path(machine.home).read_text(encoding="utf-8")
        assert text.startswith("written by Jeff Posey from the web UI at ")


# ----- over HTTP -----------------------------------------------------------------------


CONFIG = {
    "schema_version": 1,
    "project_name": "Sandbox",
    "tasks_directory": "tasks",
    "actors": [
        {"name": "Jeff Posey", "kind": "human"},
        {"name": "claude", "kind": "agent"},
    ],
    "default_user": "Jeff Posey",
}

LOOPBACK = "127.0.0.1"


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """One project and one AgentJobs home with **no** dispatch.yaml at all.

    Unconfigured on purpose: the stop has to work on a machine where dispatch was never
    set up, since that is a machine somebody may still be running agents on by hand.
    """
    (tmp_path / "tasks").mkdir()
    (tmp_path / ".agentjobs").mkdir()
    (tmp_path / "home").mkdir()
    (tmp_path / ".agentjobs" / "config.yaml").write_text(yaml.safe_dump(CONFIG), encoding="utf-8")
    monkeypatch.setenv("AGENTJOBS_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENTJOBS_TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setenv(HOME_ENV, str(tmp_path / "home"))
    reset_dependency_cache()
    yield tmp_path
    reset_dependency_cache()


def owner() -> TestClient:
    return TestClient(app, client=(LOOPBACK, 51000))


def dispatched(sandbox: Path) -> TestClient:
    """A client the server resolves as a live dispatched run, with a real minted credential."""
    home = sandbox / "home"
    run_id = new_run_id()
    directory = RunDirectory.create(
        home,
        run_id,
        {
            "run_id": run_id,
            "task_id": "task-001-sample",
            "project_id": "_local",
            "mode": "session",
            "agent": "claude",
            "status": "running",
        },
    )
    token = mint_run_credential(directory.path, run_id)
    assert token, "the run credential must actually mint, or nothing below is tested"
    return TestClient(app, client=(LOOPBACK, 51000), headers={RUN_CREDENTIAL_HEADER: token})


class TestOverHttp:
    def test_the_owner_stops_and_resumes_an_unconfigured_machine(self, sandbox: Path) -> None:
        client = owner()
        assert client.get("/api/runs/emergency-stop").json()["stopped"] is False

        pressed = client.post("/api/runs/emergency-stop")

        assert pressed.status_code == 200, pressed.text
        body = pressed.json()
        assert body["stopped"] is True
        assert "from the web UI" in body["note"]
        assert sentinel_active(sandbox / "home")
        assert client.get("/api/runs/emergency-stop").json()["stopped"] is True

        resumed = client.post("/api/runs/emergency-stop/resume")

        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["stopped"] is False
        assert not sentinel_active(sandbox / "home")

    def test_a_run_may_neither_press_it_nor_lift_it(self, sandbox: Path) -> None:
        run = dispatched(sandbox)

        pressed = run.post("/api/runs/emergency-stop")
        assert pressed.status_code == 403, pressed.text
        assert pressed.json()["code"] == "capability_denied"
        assert not sentinel_active(sandbox / "home")

        sentinel_path(sandbox / "home").write_text("down\n", encoding="utf-8")
        lifted = run.post("/api/runs/emergency-stop/resume")
        assert lifted.status_code == 403, lifted.text
        assert lifted.json()["code"] == "capability_denied"
        assert sentinel_active(sandbox / "home")

    def test_pressing_needs_dispatch_and_lifting_needs_dispatch_admin(self) -> None:
        assert ROUTE_CAPABILITIES["press_emergency_stop"].capability is Capability.DISPATCH
        assert (
            ROUTE_CAPABILITIES["resume_after_emergency_stop"].capability
            is Capability.DISPATCH_ADMIN
        )
