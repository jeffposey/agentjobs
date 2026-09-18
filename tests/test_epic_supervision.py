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
from datetime import datetime, timezone
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
from agentjobs.execution import store as epic_store
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


SIBLING_DISPATCH = r"""
import pathlib, sys
sys.path.insert(0, sys.argv[4])
from support import task_store
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import DispatchTrigger
from agentjobs.projects import ProjectRegistry
from agentjobs.dispatch.guards import DispatchRequest, dispatch_task

home, child_id, epic = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3] == "epic"
project = ProjectRegistry(home=home).get("sandbox")
manager = TaskManager(task_store(project.root / "tasks", project_id="sandbox"))
if epic:
    request = DispatchRequest(
        task_id=child_id, trigger=DispatchTrigger.CHILD, on_behalf_of_parent=True
    )
else:
    request = DispatchRequest(
        task_id=child_id, authorized_by="Jeff Posey", authorization_note="Mine."
    )
handle = dispatch_task(
    manager=manager, project=project, project_config=project.load_config(),
    request=request, home=home, api_base="http://127.0.0.1:9",
)
print(handle.run_id)
"""

LIVE_WALKER = r"""
import os, pathlib, sys, time
sys.path.insert(0, sys.argv[4])
from support import task_store
from agentjobs.manager import TaskManager
from agentjobs.projects import ProjectRegistry
from agentjobs.dispatch import epic

home, parent_id, signals = pathlib.Path(sys.argv[1]), sys.argv[2], pathlib.Path(sys.argv[3])
project = ProjectRegistry(home=home).get("sandbox")
manager = TaskManager(task_store(project.root / "tasks", project_id="sandbox"))

def sleep(_seconds):
    (signals / "walking").write_text(str(os.getpid()))
    deadline = time.monotonic() + 120
    while not (signals / "go").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    sys.stdout.flush()
    os._exit(0)

epic.walk_epic(
    manager=manager, project=project, project_config=project.load_config(),
    parent_id=parent_id, home=home, api_base="http://127.0.0.1:9",
    settings=epic.WalkSettings(poll_seconds=0.0, child_timeout_seconds=600.0, max_concurrent=1),
    sleep=sleep,
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
        dispatch: Optional[Callable[..., Any]] = None,
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
            dispatch=dispatch,
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
        """Every listed session working one child.

        A name is ``<project>/<task>``, and carries ``#<n>`` only when a session of that
        name was already live (task-452) -- so the task is everything before the first
        ``#``, matched as a whole trailing segment because ``task-1`` is a prefix of
        ``task-10``.
        """
        return [
            row
            for row in self.machine.rows()
            if str(row.get("name")).partition("#")[0].endswith(f"/{child_id}")
        ]

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

    def test_a_recycled_holder_pid_does_not_refuse_the_walk_forever(self, walk: Epic) -> None:
        """task-444: the gate recycled a crashed walker's pid, and its resume was refused.

        A process that started after the walk last wrote cannot be the walk's holder,
        however alive it is. This test's own interpreter plays the newcomer.
        """
        only = walk.child("First")
        store = journal(walk.machine.home)
        parent = walk.machine.manager.get_task(walk.parent_id)
        assert parent is not None
        entry = epic.parent_authorizing_entry(parent)
        assert entry is not None
        opened, _ = store.open_walk(
            project_id="sandbox",
            parent_task_id=walk.parent_id,
            authority_entry=entry.id,
            authority_actor=entry.actor,
            settings={},
            holder="another-host:1",
            holder_pid=os.getpid(),
        )
        with store.transaction("test: the holder last wrote long ago") as connection:
            connection.execute(
                "UPDATE supervision SET updated_at = ? WHERE walk_id = ?",
                ("2000-01-01T00:00:00+00:00", opened.walk_id),
            )
        assert epic_store.process_created_after(
            os.getpid(), datetime(2000, 1, 1, tzinfo=timezone.utc)
        )
        assert not epic_store.process_created_after(os.getpid(), datetime.now(timezone.utc))

        result = walk.walk(on_sleep=lambda _n: walk.complete_active())
        assert result is not None and result.stop is WalkStop.ALL_CHILDREN_DONE, result.detail
        assert len(walk.sessions_named(only)) == 1


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


# ----- a restarted walk with no record of its own (task-416 entry 19) --------------------


def test_a_walk_with_no_record_adopts_children_already_flying_then_starts_what_waits_on_them(
    walk: Epic,
) -> None:
    from agentjobs.dispatch.guards import DispatchRequest, dispatch_task
    from agentjobs.models_v2 import DispatchTrigger

    first = walk.child("First")
    second = walk.child("Second")
    walk.machine.manager.update_task(
        second, actor="claude", dependencies=[{"task": first, "type": "needs"}]
    )
    project = ProjectRegistry(home=walk.machine.home).get("sandbox")
    # What a walk from before this build left behind: a child dispatched on the epic's
    # authorisation, flying, with no supervision record anywhere.
    dispatch_task(
        manager=walk.machine.manager,
        project=project,
        project_config=project.load_config(),
        request=DispatchRequest(
            task_id=first, trigger=DispatchTrigger.CHILD, on_behalf_of_parent=True
        ),
        home=walk.machine.home,
        api_base="http://127.0.0.1:9",
    )
    assert journal(walk.machine.home).open_walks() == []

    def on_sleep(tick: int) -> None:
        if tick == 1:
            assert walk.sessions_named(second) == [], "the dependent child waits"
        walk.complete_active()

    result = walk.walk(on_sleep=on_sleep)
    assert result is not None and result.stop is WalkStop.ALL_CHILDREN_DONE
    assert len(walk.sessions_named(first)) == 1, "adopted, not dispatched again"
    assert walk.epic_attempts(first) == 1
    assert len(walk.sessions_named(second)) == 1
    assert [a.child_id for a in result.attempts] == [first, second]


# ----- task-444: two walkers of one epic ---------------------------------------------


class TestTwoWalkersOfOneEpic:
    """On 2026-09-13 a second walk of one epic ran beside the first, and the first counted
    the second's healthy child dead and grounded the epic on it."""

    def sibling(self, walk: Epic, child_id: str, *, epic_authority: bool) -> str:
        """Dispatch ``child_id`` from another interpreter, as a second walk would."""
        done = subprocess.run(
            [
                sys.executable,
                "-c",
                SIBLING_DISPATCH,
                str(walk.machine.home),
                child_id,
                "epic" if epic_authority else "own",
                str(TESTS),
            ],
            capture_output=True,
            text=True,
            env=environment(walk.machine.home),
            timeout=180,
        )
        assert done.returncode == 0, done.stderr
        return done.stdout.strip().splitlines()[-1]

    def racing(self, walk: Epic, child_id: str, *, epic_authority: bool) -> Callable[..., Any]:
        """The walk's own dispatch, beaten to ``child_id`` by another process's."""
        sibling_runs: List[str] = []

        def dispatch(**kwargs: Any) -> Any:
            if kwargs["request"].task_id == child_id and not sibling_runs:
                # The frontier was read before the other process claimed the child, which
                # is exactly the window the double walk was refused in.
                sibling_runs.append(self.sibling(walk, child_id, epic_authority=epic_authority))
            from agentjobs.dispatch.guards import dispatch_task

            return dispatch_task(**kwargs)

        dispatch.sibling_runs = sibling_runs  # type: ignore[attr-defined]
        return dispatch

    @pytest.mark.parametrize("lands_on_tick", [2, 1], ids=["while-flying", "already-closed"])
    def test_a_childs_run_started_by_another_process_on_this_authorisation_is_adopted(
        self, walk: Epic, lands_on_tick: int
    ) -> None:
        first = walk.child("First")
        second = walk.child("Second")
        dispatch = self.racing(walk, first, epic_authority=True)

        def on_sleep(tick: int) -> None:
            # Tick 1 closes the child before the walk has looked at the refusal again:
            # the other run finished first, and its verdict must still land here.
            if tick >= lands_on_tick:
                walk.complete_active()

        result = walk.walk(on_sleep=on_sleep, dispatch=dispatch)
        [sibling_run] = dispatch.sibling_runs  # type: ignore[attr-defined]
        assert result is not None and result.stop is WalkStop.ALL_CHILDREN_DONE, result.detail
        assert len(walk.sessions_named(first)) == 1, "followed, never started a second time"
        assert walk.epic_attempts(first) == 1, "the refused attempt spent nothing"
        landed = {a.child_id: a for a in result.attempts}
        assert landed[first].run_id == sibling_run
        assert landed[first].verdict.value == "completed"
        assert all(a.verdict.value == "completed" for a in result.attempts)
        assert len(walk.sessions_named(second)) == 1
        parent = walk.machine.manager.get_task(walk.parent_id)
        assert parent is not None and parent.ball is not Ball.HUMAN

    def test_a_childs_run_on_somebody_elses_authorisation_still_grounds(self, walk: Epic) -> None:
        only = walk.child("First")
        walk.machine.manager.add_log_entry(
            only, actor="Jeff Posey", type=LogEntryType.NOTE, body="I will run this one."
        )
        dispatch = self.racing(walk, only, epic_authority=False)
        result = walk.walk(on_sleep=lambda _tick: None, dispatch=dispatch)
        [sibling_run] = dispatch.sibling_runs  # type: ignore[attr-defined]
        assert result is not None and result.stop is WalkStop.COULD_NOT_START_CHILD
        assert sibling_run in result.detail and "not dispatched on this epic" in result.detail
        assert walk.epic_attempts(only) == 0
        assert len(walk.sessions_named(only)) == 1

    def test_a_second_walk_of_a_live_walk_refuses_and_only_one_fill_loop_acts(
        self, walk: Epic, tmp_path: Path
    ) -> None:
        first = walk.child("First")
        second = walk.child("Second")
        signals = tmp_path / "signals"
        signals.mkdir()
        walker = subprocess.Popen(
            [
                sys.executable,
                "-c",
                LIVE_WALKER,
                str(walk.machine.home),
                walk.parent_id,
                str(signals),
                str(TESTS),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment(walk.machine.home),
        )
        try:
            deadline = time.monotonic() + 120
            while not (signals / "walking").exists():
                assert walker.poll() is None, walker.communicate()
                assert time.monotonic() < deadline, "the first walk never took off"
                time.sleep(0.05)
            before = walk.machine.rows()
            parent_before = walk.machine.manager.get_task(walk.parent_id)
            assert parent_before is not None

            result = walk.walk()

            assert result is not None and result.stop is WalkStop.ALREADY_SUPERVISED
            # The interpreter's own pid, which on Windows is not the launcher Popen names.
            walker_pid = (signals / "walking").read_text()
            assert f"pid {walker_pid} " in result.detail and "opened " in result.detail
            assert walk.machine.rows() == before, "the second walk launched nothing"
            assert len(walk.sessions_named(first)) == 1 and walk.sessions_named(second) == []

            epic.record_walk_outcome(
                walk.machine.manager, walk.parent_id, actor="claude", result=result
            )
            walk.machine.manager.storage.refresh()
            parent_after = walk.machine.manager.get_task(walk.parent_id)
            assert parent_after is not None
            assert parent_after.ball is parent_before.ball, "the refused walk handed nothing"
            assert len(parent_after.log) == len(parent_before.log), "and wrote nothing"
        finally:
            (signals / "go").write_text("")
            walker.communicate(timeout=120)
