"""Durable epic supervision: the walk survives its own process (task-416, from task-418).

The walk here is the production ``walk_epic`` dispatching through the production
``dispatch_task`` onto the fake session CLI ``test_execution_controller`` holds to the real
driver's shape. A supervisor "dies" either as a real child interpreter ending in
``os._exit`` at one boundary, or -- where the test must stay in one process -- as an
exception escaping the loop, which leaves the record exactly as a death would: nothing in
``walk_epic`` catches it or writes on the way out.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

from agentjobs.dispatch import epic
from agentjobs.dispatch.epic import (
    WalkSettings,
    WalkStop,
    advance_hosted_walks,
    count_attempts,
    detach_walk,
    supervisor_slot_held,
    walk_epic,
)
from agentjobs.dispatch.journal import journal
from agentjobs.dispatch.ledger import read_run
from agentjobs.dispatch.runner import runs_root
from agentjobs.models_v2 import Ball, BallReason, Lifecycle, LogEntryType, Outcome
from agentjobs.projects import ProjectRegistry
from test_execution_controller import TESTS, Machine, environment

WALK_CHILD = r"""
import os, pathlib, sys
sys.path.insert(0, sys.argv[4])
from support import task_store
from agentjobs.manager import TaskManager
from agentjobs.projects import ProjectRegistry
from agentjobs.dispatch import epic
import agentjobs.dispatch.guards as guards
from agentjobs.execution.store import ExecutionStore

home, parent_id, point = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
project = ProjectRegistry(home=home).get("sandbox")
manager = TaskManager(task_store(project.root / "tasks", project_id="sandbox"))

def die(*args, **kwargs):
    sys.stdout.flush()
    os._exit(9)

if point == "before_admission":
    guards.dispatch_task = die
elif point == "after_admission":
    real = ExecutionStore.record_child
    def record(self, walk_id, **kwargs):
        if kwargs.get("status") == "flying":
            die()
        return real(self, walk_id, **kwargs)
    ExecutionStore.record_child = record

epic.walk_epic(
    manager=manager, project=project, project_config=project.load_config(),
    parent_id=parent_id, home=home, api_base="http://127.0.0.1:9",
    settings=epic.WalkSettings(poll_seconds=0.0, child_timeout_seconds=600.0, max_concurrent=1),
    sleep=lambda seconds: die(),
)
"""


class Died(BaseException):
    """Escapes the walk the way a death would: nothing in the loop catches it."""


class Epic:
    def __init__(self, machine: Machine) -> None:
        self.machine = machine
        manager = machine.manager
        parent = manager.create_task(
            title="Epic",
            category="general",
            summary="An epic.",
            description="Walk it.",
            lifecycle=Lifecycle.READY,
            actor="Jeff Posey",
        )
        manager.claim_task(parent.id, agent="claude")
        manager.add_log_entry(
            parent.id, actor="Jeff Posey", type=LogEntryType.NOTE, body="Walk it."
        )
        self.parent_id = parent.id
        self.children: List[str] = []

    def child(self, title: str) -> str:
        created = self.machine.manager.create_task(
            title=title,
            category="general",
            summary=f"{title}.",
            description=f"Do {title}.",
            lifecycle=Lifecycle.READY,
            actor="claude",
            parent=self.parent_id,
        )
        self.children.append(created.id)
        return created.id

    def status_of(self, run_id: str) -> Optional[str]:
        """A run reads finished once its task is closed; running otherwise."""
        record = read_run(runs_root(self.machine.home) / run_id)
        task = self.machine.manager.get_task(record.task_id)
        return "finished" if task is not None and not task.is_open else "running"

    def walk(
        self,
        *,
        on_sleep: Callable[[int], None] = lambda _tick: None,
        max_concurrent: int = 1,
        once: bool = False,
    ):
        ticks = {"n": 0}

        def sleep(_seconds: float) -> None:
            ticks["n"] += 1
            if ticks["n"] > 200:
                raise AssertionError("the walk did not end")
            on_sleep(ticks["n"])

        project = ProjectRegistry(home=self.machine.home).get("sandbox")
        return walk_epic(
            manager=self.machine.manager,
            project=project,
            project_config=project.load_config(),
            parent_id=self.parent_id,
            home=self.machine.home,
            api_base="http://127.0.0.1:9",
            settings=WalkSettings(
                poll_seconds=0.0, child_timeout_seconds=600.0, max_concurrent=max_concurrent
            ),
            read_run_status=self.status_of,
            sleep=sleep,
            once=once,
        )

    def complete_active(self) -> None:
        for child_id in self.children:
            task = self.machine.manager.get_task(child_id)
            if task is not None and task.lifecycle is Lifecycle.ACTIVE and task.ball is Ball.AGENT:
                self.machine.manager.close_task(child_id, actor="claude", outcome=Outcome.COMPLETED)

    def crash(self, point: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                "-c",
                WALK_CHILD,
                str(self.machine.home),
                self.parent_id,
                point,
                str(TESTS),
            ],
            capture_output=True,
            text=True,
            env=environment(self.machine.home),
            timeout=180,
        )

    def sessions_named(self, child_id: str) -> List[Dict[str, object]]:
        return [row for row in self.machine.rows() if f"/{child_id}@" in str(row.get("name"))]

    def epic_attempts(self, child_id: str) -> int:
        task = self.machine.manager.get_task(child_id)
        assert task is not None
        parent = self.machine.manager.get_task(self.parent_id)
        assert parent is not None
        entry = epic.parent_authorizing_entry(parent)
        assert entry is not None
        return count_attempts(task, parent_id=self.parent_id, entry_id=entry.id)


@pytest.fixture
def walk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Epic:
    machine = Machine(tmp_path, monkeypatch)
    machine.configure(controller="shadow")
    return Epic(machine)


# ----- epic-1: a supervisor dying around a child's admission ----------------------------


class TestSupervisorDeath:
    def test_death_before_admission_returns_the_reservation_and_starts_the_child_once(
        self, walk: Epic
    ) -> None:
        only = walk.child("First")
        died = walk.crash("before_admission")
        assert died.returncode == 9, died.stderr
        assert walk.machine.rows() == [] and journal(walk.machine.home).live_attempts() == []
        [record] = journal(walk.machine.home).open_walks()
        [child] = journal(walk.machine.home).supervised_children(record.walk_id)
        assert child.status == "admitting" and child.attempts_reserved == 1

        result = walk.walk(on_sleep=lambda _n: walk.complete_active())
        assert result is not None and result.stop is WalkStop.ALL_CHILDREN_DONE
        assert len(walk.sessions_named(only)) == 1
        assert walk.epic_attempts(only) == 1, "the dead supervisor's reservation was not spent"
        [child] = journal(walk.machine.home).supervised_children(record.walk_id)
        assert child.attempts_reserved == 1

    def test_death_after_admission_follows_the_same_child_run_and_spends_nothing_more(
        self, walk: Epic
    ) -> None:
        only = walk.child("First")
        died = walk.crash("after_admission")
        assert died.returncode == 9, died.stderr
        [session] = walk.sessions_named(only)
        [attempt] = [a for a in journal(walk.machine.home).live_attempts() if a.task_id == only]
        assert attempt.operation_id and attempt.operation_id.endswith(f":{only}:1")

        followed: List[str] = []

        def on_sleep(_n: int) -> None:
            followed.extend(a.run_id for a in journal(walk.machine.home).live_attempts())
            walk.complete_active()

        result = walk.walk(on_sleep=on_sleep)
        assert result is not None and result.stop is WalkStop.ALL_CHILDREN_DONE
        assert walk.sessions_named(only) == [session], "no second launch"
        assert followed and set(followed) == {attempt.run_id}, "the exact run was followed"
        assert walk.epic_attempts(only) == 1
        assert [a.run_id for a in result.attempts] == [attempt.run_id]

    def test_a_live_supervisor_is_never_joined_by_a_second(self, walk: Epic) -> None:
        walk.child("First")
        store = journal(walk.machine.home)
        parent = walk.machine.manager.get_task(walk.parent_id)
        assert parent is not None
        entry = epic.parent_authorizing_entry(parent)
        assert entry is not None
        store.open_walk(
            project_id="sandbox",
            parent_task_id=walk.parent_id,
            authority_entry=entry.id,
            authority_actor=entry.actor,
            settings={},
            holder="another-host:1",
            holder_pid=os.getpid(),
        )
        result = walk.walk()
        assert result is not None and result.stop is WalkStop.ALREADY_SUPERVISED
        assert walk.machine.rows() == []


# ----- epic-2: a child that lands while it is being registered ---------------------------


def test_a_child_finishing_during_its_registration_is_landed_not_lost(
    walk: Epic, monkeypatch: pytest.MonkeyPatch
) -> None:
    only = walk.child("Quick")
    real = epic._Supervision.take_off

    def take_off(self: Any, flight: Any, **kwargs: Any) -> Any:
        # The child's own finish lands between the dispatch returning and the walk
        # recording the flight: the window a subscribe-then-read loop can lose.
        walk.machine.manager.close_task(only, actor="claude", outcome=Outcome.COMPLETED)
        return real(self, flight, **kwargs)

    monkeypatch.setattr(epic._Supervision, "take_off", take_off)
    result = walk.walk()
    assert result is not None and result.stop is WalkStop.ALL_CHILDREN_DONE
    assert [(a.child_id, a.verdict.value) for a in result.attempts] == [(only, "completed")]


# ----- epic-3: grounding outlives the supervisor ----------------------------------------


def test_grounding_survives_a_restart_and_lets_flying_siblings_land(walk: Epic) -> None:
    first = walk.child("First")
    second = walk.child("Second")
    third = walk.child("Third")

    def park_first_then_die(tick: int) -> None:
        if tick == 1:
            walk.machine.manager.handoff(
                first,
                actor="claude",
                ball=Ball.HUMAN,
                ball_reason=BallReason.REVIEW,
                ball_prompt="Look at this.",
            )
        elif tick == 2:
            raise Died()

    with pytest.raises(Died):
        walk.walk(on_sleep=park_first_then_die, max_concurrent=2)
    [record] = journal(walk.machine.home).open_walks()
    assert record.grounding and record.grounding["stop"] == "child_needs_a_human"

    result = walk.walk(on_sleep=lambda _n: walk.complete_active(), max_concurrent=2)
    assert result is not None and result.stop is WalkStop.CHILD_NEEDS_A_HUMAN
    landed = {a.child_id: a.verdict.value for a in result.attempts}
    assert landed == {first: "parked", second: "completed"}
    assert walk.sessions_named(third) == [], "no takeoff after grounding, even after a restart"


# ----- epic-4: the two-attempt bound across a restart -----------------------------------


def test_two_attempts_per_child_hold_across_a_restart(walk: Epic) -> None:
    only = walk.child("Fragile")
    statuses: Dict[str, str] = {}

    def status(run_id: str) -> Optional[str]:
        return statuses.get(run_id, "running")

    def kill_current(tick: int) -> None:
        time.sleep(1.1)  # the per-task cooldown binds a child retry, as it should
        for attempt in journal(walk.machine.home).live_attempts():
            statuses[attempt.run_id] = "failed"
            journal(walk.machine.home).conclude(
                attempt.run_id, outcome="interrupted", status="failed", concluded_by="test"
            )
        if tick == 2:
            raise Died()

    walk.machine.configure(controller="shadow", limits={"auto": {"cooldown_seconds": 1}})
    walk.status_of = status  # type: ignore[method-assign]
    with pytest.raises(Died):
        walk.walk(on_sleep=kill_current)
    result = walk.walk(on_sleep=kill_current)
    assert result is not None and result.stop is WalkStop.CHILD_EXHAUSTED_ATTEMPTS
    assert len(walk.sessions_named(only)) == 2
    assert walk.epic_attempts(only) == 2


# ----- epic-5: a wait no session holds ---------------------------------------------------


def test_a_detached_walk_is_advanced_by_the_server_with_no_supervisor_process(walk: Epic) -> None:
    first = walk.child("First")
    project = ProjectRegistry(home=walk.machine.home).get("sandbox")
    walk_id = detach_walk(
        manager=walk.machine.manager,
        project=project,
        parent_id=walk.parent_id,
        home=walk.machine.home,
        settings=WalkSettings(max_concurrent=1),
        posture=None,
        actor="claude",
    )

    def resolve(project_id: str) -> Any:
        return walk.machine.manager, project

    lines = advance_hosted_walks(walk.machine.home, resolve=resolve)
    assert len(walk.sessions_named(first)) == 1, lines
    assert advance_hosted_walks(walk.machine.home, resolve=resolve) is not None
    assert len(walk.sessions_named(first)) == 1, "a second tick follows, it does not relaunch"

    walk.complete_active()
    for run in [row for row in walk.machine.rows()]:
        run["state"] = "done"
    lines = advance_hosted_walks(walk.machine.home, resolve=resolve)
    assert any("all_children_done" in line for line in lines), lines
    record = journal(walk.machine.home).walk(walk_id)
    assert record is not None and record.state == "done"
    parent = walk.machine.manager.get_task(walk.parent_id)
    assert (
        parent is not None and parent.is_open
    ), "the parent's own acceptance is not the walk's call"
    assert "every open child is done" in (parent.log[-1].body or "")
    assert advance_hosted_walks(walk.machine.home, resolve=resolve) == []


# ----- from-walk-slots ------------------------------------------------------------------


def test_a_supervisor_that_is_a_dispatched_run_counts_its_own_slot(
    walk: Epic, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = walk.machine.task()
    handle = walk.machine.dispatch(task_id)
    monkeypatch.setenv("AGENTJOBS_RUN_ID", handle.run_id)
    assert supervisor_slot_held(walk.machine.home)
    monkeypatch.setenv("AGENTJOBS_RUN_ID", "run_nobody")
    assert not supervisor_slot_held(walk.machine.home)


def test_a_landed_child_whose_run_has_not_settled_holds_its_slot(walk: Epic) -> None:
    first = walk.child("First")
    second = walk.child("Second")
    started_while_settling: List[bool] = []
    settled = {"done": False}

    def status(run_id: str) -> Optional[str]:
        record = read_run(runs_root(walk.machine.home) / run_id)
        if record.task_id == first:
            return "finished" if settled["done"] else "running"
        return walk_status_default(run_id)

    walk_status_default: Callable[[str], Optional[str]] = walk.status_of

    def on_sleep(tick: int) -> None:
        if tick == 1:
            walk.machine.manager.close_task(first, actor="claude", outcome=Outcome.COMPLETED)
        elif tick == 3:
            started_while_settling.append(bool(walk.sessions_named(second)))
            settled["done"] = True
        elif tick > 4:
            walk.complete_active()

    walk.status_of = status  # type: ignore[method-assign]
    result = walk.walk(on_sleep=on_sleep)
    assert result is not None and result.stop is WalkStop.ALL_CHILDREN_DONE
    assert started_while_settling == [False], "nothing took off into the unsettled slot"
    assert len(walk.sessions_named(second)) == 1
