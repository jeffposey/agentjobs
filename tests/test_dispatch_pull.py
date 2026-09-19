"""The pull mode, against production dispatch (task-462).

Nothing here stubs a gate, for the reason ``test_dispatch_queue.py`` gives: the whole
claim of a clickless trigger is that its runs are judged by the same chain as a click's,
and a test that mocked the chain would be asserting the claim instead of checking it.
Every start below goes through ``guards.dispatch_task`` -- the real guards, the real
runner, the real journal -- driven by the real ``Controller.tick()`` on task-416's
``Machine`` harness.

Three things are deliberately not faked, and they are what the acceptance criteria turn
on:

* **The attribution** (ac-2). ``assert_human_clocked`` is run against the entry the pull
  path wrote, read back off storage, rather than against the object it was handed.
* **The order** (ac-1). The tasks are moved in the *stored queue* and the mode is
  expected to follow, which is the difference between reading ``task_next`` and reading
  a timestamp.
* **The precedence** (ac-3). A real queued dispatch is enqueued through
  ``dispatch_or_queue`` and a real slot is freed, and the pull mode is expected to leave
  it alone.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

import pytest

from agentjobs.dispatch import pull as dispatch_pull
from agentjobs.dispatch import queue as dispatch_queue
from agentjobs.dispatch.guards import (
    IF_FULL_QUEUE,
    ConflictingAuthorizationError,
    DispatchRequest,
    assert_human_clocked,
    dispatch_task,
)
from agentjobs.dispatch.journal import journal
from agentjobs.dispatch.ledger import live_runs
from agentjobs.dispatch.queue import dispatch_or_queue
from agentjobs.execution.factory import close_execution_stores
from agentjobs.execution.store import (
    BOUND_OPEN,
    BOUND_STARTS,
    BOUND_UNTIL,
    PULL_ARMED,
    PULL_DISARMED,
    PULL_EXPIRED,
    PULL_FAULTED,
    PULL_SPENT,
)
from agentjobs.models_v2 import DispatchTrigger, Lifecycle, LogEntryType
from agentjobs.projects import ProjectRegistry

from test_execution_controller import Machine, machine

__all__ = ["machine"]  # a fixture, imported by name -- the harness is task-416's


ARMER = "Jeff Posey"


# ----- helpers ------------------------------------------------------------------


def arm(box: Machine, **kwargs: Any) -> Any:
    project = ProjectRegistry(home=box.home).get("sandbox")
    kwargs.setdefault("bound_kind", BOUND_OPEN)
    return dispatch_pull.arm(box.home, project, project.load_config(), armed_by=ARMER, **kwargs)


def run_id_for(box: Machine, task_id: str) -> str:
    """The live run holding this task, from the ledger the ceiling is counted from."""
    for record in live_runs(box.home):
        if record.task_id == task_id:
            return record.run_id
    raise AssertionError(f"{task_id} has no live run")


def free_slot(box: Machine, task_id: str) -> None:
    """End the run holding this task's slot, in the place the ceiling reads.

    ``effective_live_runs`` asks the journal whether each run directory is still live and
    believes that over the directory's own meta, so concluding the attempt *is* freeing
    the slot in the one place the guard looks.
    """
    journal(box.home).conclude(
        run_id_for(box, task_id), outcome="completed", status="finished", concluded_by="test"
    )


def started_tasks(box: Machine) -> List[str]:
    """Every task a run was started on, in the order the fake runner saw them."""
    return [str(row["name"]).rsplit("/", 1)[-1] for row in box.rows()]


def pulled_entry(box: Machine, task_id: str) -> Any:
    """The authorising entry the pull path wrote onto this task, read back off storage."""
    task = box.manager.get_task(task_id)
    assert task is not None
    for entry in reversed(task.log):
        if entry.type is LogEntryType.NOTE and (entry.data or {}).get("pull"):
            return entry
    raise AssertionError(f"{task_id} has no pull authorisation entry")


def dispatch_entry(box: Machine, task_id: str) -> Any:
    task = box.manager.get_task(task_id)
    assert task is not None
    for entry in reversed(task.log):
        if entry.type is LogEntryType.DISPATCH:
            return entry
    raise AssertionError(f"{task_id} was never dispatched")


def notes(box: Machine, task_id: str) -> List[str]:
    task = box.manager.get_task(task_id)
    assert task is not None
    return [entry.body or "" for entry in task.log if entry.type is LogEntryType.NOTE]


def arming_row(box: Machine, arming_id: str) -> Any:
    row = journal(box.home).pull_arming(arming_id)
    assert row is not None
    return row


def thin_task(box: Machine) -> str:
    """A ready task with no ``spec.description``: filed as a title and a hope."""
    created = box.manager.create_task(
        title="No spec",
        category="general",
        summary="Nothing an agent could work from.",
        description="",
        lifecycle=Lifecycle.READY,
        actor=ARMER,
    )
    return created.id


def open_incident(box: Machine, kind: str = "usage_limit") -> None:
    """An open auth/usage incident against **this machine's own credential** (task-463).

    The profile is what makes it bite. Since the pause is judged per credential, an
    incident carrying an empty profile is an incident against nobody, and seeding one
    would leave this test green whatever the gate did.
    """
    from agentjobs.dispatch import start_pause
    from agentjobs.dispatch.config import load_dispatch_config

    config = load_dispatch_config(box.home)
    assert config is not None
    profile = start_pause.profile_for_runner(config.runners["fake"])
    moment = datetime.now(timezone.utc).isoformat()
    with journal(box.home).transaction("test-incident") as connection:
        connection.execute(
            "INSERT INTO auth_incident(incident_id, kind, profile_key, profile_json, state, "
            "opened_at, next_probe_at, updated_at) VALUES (?,?,?,?,'open',?,?,?)",
            (
                "inc_test",
                kind,
                profile.key,
                json.dumps(profile.as_json()),
                moment,
                moment,
                moment,
            ),
        )


# ----- arming ---------------------------------------------------------------------


class TestArming:
    def test_arming_starts_nothing_by_itself(self, machine: Machine) -> None:
        machine.task()
        arm(machine, bound_kind=BOUND_STARTS, bound_starts=3)

        assert machine.rows() == []

    def test_only_one_arming_per_project(self, machine: Machine) -> None:
        arm(machine)

        with pytest.raises(dispatch_pull.PullArmingError) as refused:
            arm(machine)

        assert "already armed" in str(refused.value)

    def test_an_agent_cannot_be_named_as_the_armer(self, machine: Machine) -> None:
        project = ProjectRegistry(home=machine.home).get("sandbox")

        with pytest.raises(Exception) as refused:
            dispatch_pull.arm(
                machine.home,
                project,
                project.load_config(),
                armed_by="claude",
                bound_kind=BOUND_OPEN,
            )

        assert "may not authorise a dispatch" in str(refused.value)

    def test_a_posture_above_the_ceiling_is_refused_where_someone_is_waiting(
        self, machine: Machine
    ) -> None:
        from agentjobs.dispatch.config import Posture

        machine.configure(project={"posture": "auto", "max_posture": "auto"})

        with pytest.raises(dispatch_pull.PullArmingError) as refused:
            arm(machine, posture=Posture.AUTONOMOUS)

        assert "capped at" in str(refused.value)

    def test_a_starts_bound_needs_a_number(self, machine: Machine) -> None:
        with pytest.raises(dispatch_pull.PullArmingError):
            arm(machine, bound_kind=BOUND_STARTS, bound_starts=None)

    def test_it_survives_every_process_that_wrote_it(self, machine: Machine) -> None:
        armed = arm(machine, bound_kind=BOUND_STARTS, bound_starts=2)

        close_execution_stores()

        alive = dispatch_pull.armed(machine.home, "sandbox")
        assert alive is not None and alive.arming_id == armed.arming_id


# ----- the tick starts the queue's next task, in order (ac-1) ------------------------


class TestPulling:
    def test_it_fills_free_slots_in_queue_order_and_stops_at_the_bound(
        self, machine: Machine
    ) -> None:
        machine.configure(limits={"max_concurrent_runs": 2})
        first, second, third, fourth = [machine.task() for _ in range(4)]
        arm(machine, bound_kind=BOUND_STARTS, bound_starts=3)

        # Two slots, one start per project per pass, so two passes fill the machine.
        machine.tick(2)
        assert started_tasks(machine) == [first, second]

        # Full: the third waits for a slot rather than being refused out of the bound.
        machine.tick()
        assert started_tasks(machine) == [first, second]

        free_slot(machine, first)
        machine.tick()
        assert started_tasks(machine) == [first, second, third]

        # The bound is spent. The fourth task is claimable and is not started.
        free_slot(machine, second)
        machine.tick(2)
        assert started_tasks(machine) == [first, second, third]
        assert fourth not in started_tasks(machine)
        assert dispatch_pull.armed(machine.home, "sandbox") is None

    def test_it_follows_the_stored_queue_rather_than_a_timestamp(self, machine: Machine) -> None:
        """A reorder mid-run is respected, which is the whole reason it reads `task_next`."""
        machine.configure(limits={"max_concurrent_runs": 1})
        first, second, third = [machine.task() for _ in range(3)]
        arm(machine, bound_kind=BOUND_STARTS, bound_starts=2)

        machine.tick()
        assert started_tasks(machine) == [first]

        machine.manager.move(third, top=True, actor=ARMER)
        free_slot(machine, first)
        machine.tick()

        assert started_tasks(machine) == [first, third]
        assert second not in started_tasks(machine)

    def test_the_bound_is_never_overspent_by_two_ticks(self, machine: Machine) -> None:
        """The charge and the check are one statement, so a race loses safely."""
        armed = arm(machine, bound_kind=BOUND_STARTS, bound_starts=2)
        store = journal(machine.home)

        assert store.spend_pull_start(armed.arming_id) is not None
        assert store.spend_pull_start(armed.arming_id) is not None
        assert store.spend_pull_start(armed.arming_id) is None

    def test_an_until_bound_expires_without_anything_happening(self, machine: Machine) -> None:
        machine.task()
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        armed = arm(machine, bound_kind=BOUND_UNTIL, bound_until=past)

        machine.tick()

        assert machine.rows() == []
        assert arming_row(machine, armed.arming_id).state == PULL_EXPIRED

    def test_an_empty_backlog_is_idle_rather_than_an_ending(self, machine: Machine) -> None:
        armed = arm(machine, bound_kind=BOUND_STARTS, bound_starts=3)

        machine.tick()

        assert arming_row(machine, armed.arming_id).state == "armed"


# ----- attribution (ac-2) -------------------------------------------------------------


class TestAttribution:
    def test_the_entry_names_the_arming_and_the_person(self, machine: Machine) -> None:
        machine.configure(limits={"max_concurrent_runs": 1})
        task_id = machine.task()
        armed = arm(machine, bound_kind=BOUND_STARTS, bound_starts=1)

        machine.tick()

        entry = pulled_entry(machine, task_id)
        assert entry.actor == ARMER
        assert armed.arming_id in (entry.body or "")
        assert ARMER in (entry.body or "")
        assert entry.data["pull"]["arming"] == armed.arming_id
        assert entry.data["pull"]["armed_by"] == ARMER
        assert entry.data["authorizes_dispatch"] is True

    def test_that_stored_entry_satisfies_the_human_clocked_rule(self, machine: Machine) -> None:
        """Not a bypass of section 2: the same check, on an entry read back off disk."""
        machine.configure(limits={"max_concurrent_runs": 1})
        task_id = machine.task()
        arm(machine, bound_kind=BOUND_STARTS, bound_starts=1)

        machine.tick()

        project = ProjectRegistry(home=machine.home).get("sandbox")
        actor = assert_human_clocked(project.load_config(), pulled_entry(machine, task_id))
        assert actor.is_human

    def test_the_run_is_marked_as_pulled_and_caused_by_that_entry(self, machine: Machine) -> None:
        machine.configure(limits={"max_concurrent_runs": 1})
        task_id = machine.task()
        arm(machine, bound_kind=BOUND_STARTS, bound_starts=1)

        machine.tick()

        entry = dispatch_entry(machine, task_id)
        assert entry.data["trigger"] == DispatchTrigger.PULL.value
        assert entry.data["caused_by"] == pulled_entry(machine, task_id).id

    def test_an_arming_of_another_project_authorises_nothing_here(self, machine: Machine) -> None:
        task_id = machine.task()
        journal(machine.home).arm_pull(
            "somewhere-else", armed_by=ARMER, bound_kind=BOUND_OPEN, arming_id="arm_elsewhere"
        )
        project = ProjectRegistry(home=machine.home).get("sandbox")

        with pytest.raises(ConflictingAuthorizationError) as refused:
            dispatch_task(
                manager=machine.manager,
                project=project,
                project_config=project.load_config(),
                request=DispatchRequest(task_id=task_id, pull_arming_id="arm_elsewhere"),
                home=machine.home,
                api_base="http://127.0.0.1:9",
            )

        assert "one project's backlog" in str(refused.value)

    def test_a_disarmed_arming_authorises_nothing(self, machine: Machine) -> None:
        task_id = machine.task()
        armed = arm(machine)
        dispatch_pull.disarm(machine.home, "sandbox", requester=ARMER)
        project = ProjectRegistry(home=machine.home).get("sandbox")

        with pytest.raises(dispatch_pull.NotArmedError):
            dispatch_task(
                manager=machine.manager,
                project=project,
                project_config=project.load_config(),
                request=DispatchRequest(task_id=task_id, pull_arming_id=armed.arming_id),
                home=machine.home,
                api_base="http://127.0.0.1:9",
            )

    def test_naming_an_arming_and_an_entry_is_refused(self, machine: Machine) -> None:
        task_id = machine.task()
        armed = arm(machine)
        project = ProjectRegistry(home=machine.home).get("sandbox")

        with pytest.raises(ConflictingAuthorizationError) as refused:
            dispatch_task(
                manager=machine.manager,
                project=project,
                project_config=project.load_config(),
                request=DispatchRequest(
                    task_id=task_id,
                    pull_arming_id=armed.arming_id,
                    caused_by=machine.authorised_by,
                ),
                home=machine.home,
                api_base="http://127.0.0.1:9",
            )

        assert "four different acts" in str(refused.value)


# ----- the manual queue goes first (ac-3) ---------------------------------------------


def enqueue(box: Machine, task_id: str) -> Any:
    project = ProjectRegistry(home=box.home).get("sandbox")
    return dispatch_or_queue(
        manager=box.manager,
        project=project,
        project_config=project.load_config(),
        request=DispatchRequest(
            task_id=task_id, caused_by=box.authorised_by, if_full=IF_FULL_QUEUE
        ),
        home=box.home,
        api_base="http://127.0.0.1:9",
        queued_by=ARMER,
    )


class TestPrecedence:
    def test_a_queued_dispatch_takes_the_slot_before_a_pulled_one(self, machine: Machine) -> None:
        machine.configure(limits={"max_concurrent_runs": 1})
        holding = machine.task()
        machine.dispatch(holding)
        asked_for = machine.task()
        machine.task()  # what the pull mode would otherwise reach first
        enqueue(machine, asked_for)
        arm(machine, bound_kind=BOUND_STARTS, bound_starts=3)

        # The machine is full and something is waiting: the pull mode stays out of it.
        machine.tick()
        assert started_tasks(machine) == [holding]

        free_slot(machine, holding)
        machine.tick()

        assert started_tasks(machine) == [holding, asked_for]

    def test_the_pull_mode_yields_while_anything_is_waiting(self, machine: Machine) -> None:
        """Not merely 'runs afterwards': it starts nothing at all while a queue exists."""
        machine.configure(limits={"max_concurrent_runs": 1})
        holding = machine.task()
        machine.dispatch(holding)
        queued = machine.task()
        enqueue(machine, queued)
        machine.task()
        arm(machine, bound_kind=BOUND_STARTS, bound_starts=3)

        lines = machine.tick()

        assert any("waiting for a slot and starts first" in line for line in lines)
        assert started_tasks(machine) == [holding]

    def test_the_pull_mode_never_enqueues(self, machine: Machine) -> None:
        machine.configure(limits={"max_concurrent_runs": 1})
        machine.dispatch(machine.task())
        machine.task()
        arm(machine, bound_kind=BOUND_STARTS, bound_starts=3)

        machine.tick(2)

        assert dispatch_queue.waiting(machine.home) == []


# ----- disarming, skipping and faulting (ac-4) -----------------------------------------


class TestStopping:
    def test_disarming_stops_takeoffs_and_leaves_live_runs_running(self, machine: Machine) -> None:
        machine.configure(limits={"max_concurrent_runs": 2})
        first = machine.task()
        machine.task()
        armed = arm(machine, bound_kind=BOUND_STARTS, bound_starts=3)
        machine.tick()
        assert started_tasks(machine) == [first]

        retired = dispatch_pull.disarm(machine.home, "sandbox", requester=ARMER)

        assert retired is not None and retired.state == PULL_DISARMED
        assert len(machine.live_sessions()) == 1  # the run it already started
        assert run_id_for(machine, first)  # still live, and still holding its slot
        machine.tick(2)
        assert started_tasks(machine) == [first]
        assert arming_row(machine, armed.arming_id).retired_by == ARMER

    def test_disarming_an_unarmed_project_is_quiet(self, machine: Machine) -> None:
        assert dispatch_pull.disarm(machine.home, "sandbox", requester=ARMER) is None

    def test_a_task_refused_for_its_own_reason_is_skipped_and_told_why(
        self, machine: Machine
    ) -> None:
        machine.configure(limits={"max_concurrent_runs": 1})
        thin = thin_task(machine)
        good = machine.task()
        arm(machine, bound_kind=BOUND_STARTS, bound_starts=2)

        machine.tick()

        assert started_tasks(machine) == [good]
        assert any("could not start it" in body for body in notes(machine, thin))

    def test_a_skipped_task_does_not_spend_the_bound(self, machine: Machine) -> None:
        machine.configure(limits={"max_concurrent_runs": 1})
        thin_task(machine)
        machine.task()
        armed = arm(machine, bound_kind=BOUND_STARTS, bound_starts=2)

        machine.tick()

        assert arming_row(machine, armed.arming_id).started == 1

    def test_a_dirty_tree_stops_the_pass_without_writing_on_anyone(self, machine: Machine) -> None:
        machine.configure(limits={"max_concurrent_runs": 2}, project={"require_clean_tree": True})
        (machine.root / "scratch.txt").write_text("in-flight work", encoding="utf-8")
        first = machine.task()
        second = machine.task()
        arm(machine, bound_kind=BOUND_STARTS, bound_starts=3)

        machine.tick()

        assert machine.rows() == []
        assert not any("could not start it" in body for body in notes(machine, first))
        assert not any("could not start it" in body for body in notes(machine, second))

    def test_three_failed_launches_in_a_row_disarm_it(self, machine: Machine) -> None:
        machine.configure(limits={"max_concurrent_runs": 4})
        for _ in range(5):
            machine.task()
        armed = arm(machine, bound_kind=BOUND_STARTS, bound_starts=10)
        machine.fake_cli.write_text("import sys\nsys.exit(9)\n", encoding="utf-8")

        machine.tick()

        row = arming_row(machine, armed.arming_id)
        assert row.state == PULL_FAULTED
        assert "in a row failed to launch" in row.detail

    def test_a_failed_launch_does_not_spend_the_bound(self, machine: Machine) -> None:
        machine.configure(limits={"max_concurrent_runs": 4})
        machine.task()
        armed = arm(machine, bound_kind=BOUND_STARTS, bound_starts=5)
        machine.fake_cli.write_text("import sys\nsys.exit(9)\n", encoding="utf-8")

        machine.tick()

        assert arming_row(machine, armed.arming_id).started == 0

    def test_an_open_auth_incident_holds_the_arming_rather_than_retiring_it(
        self, machine: Machine
    ) -> None:
        """task-463 replaced the disarm this seam used to do with a pause.

        Retiring threw the authority away, so the hours after the reset -- exactly the
        hours the arming was for -- were lost unless somebody came back and armed it
        again. The full behaviour, including the resume, is in ``test_start_pause.py``;
        what is asserted here is only that this module no longer retires.
        """
        machine.task()
        armed = arm(machine, bound_kind=BOUND_STARTS, bound_starts=3)
        open_incident(machine)

        lines = machine.tick()

        row = arming_row(machine, armed.arming_id)
        assert machine.rows() == []
        assert row.state == PULL_ARMED
        assert row.started == 0
        assert any("paused" in line and "usage limit" in line for line in lines)


# ----- the bound's own arithmetic ------------------------------------------------------


class TestBoundSentence:
    def test_a_starts_bound_reads_as_a_count(self, machine: Machine) -> None:
        armed = arm(machine, bound_kind=BOUND_STARTS, bound_starts=3)
        assert dispatch_pull.bound_sentence(armed) == "0 of 3 starts used"

    def test_an_open_bound_says_what_ends_it(self, machine: Machine) -> None:
        assert dispatch_pull.bound_sentence(arm(machine)) == "until disarmed"

    def test_an_until_bound_names_the_moment(self, machine: Machine) -> None:
        armed = arm(machine, bound_kind=BOUND_UNTIL, bound_until="2030-01-01T00:00:00+00:00")
        assert "2030-01-01" in dispatch_pull.bound_sentence(armed)

    def test_a_spent_arming_reports_none_left(self, machine: Machine) -> None:
        armed = arm(machine, bound_kind=BOUND_STARTS, bound_starts=1)
        charged = journal(machine.home).spend_pull_start(armed.arming_id)
        assert charged is not None and charged.starts_left == 0
        journal(machine.home).retire_pull(armed.arming_id, state=PULL_SPENT)
        assert arming_row(machine, armed.arming_id).state == PULL_SPENT


def next_preview(box: Machine) -> Optional[str]:
    task = dispatch_pull.next_task(box.manager)
    return task.id if task else None


class TestWhatStartsNext:
    def test_the_preview_is_the_queues_own_answer(self, machine: Machine) -> None:
        first = machine.task()
        machine.task()
        arm(machine)

        assert next_preview(machine) == first

    def test_the_preview_follows_a_reorder(self, machine: Machine) -> None:
        machine.task()
        second = machine.task()
        arm(machine)
        machine.manager.move(second, top=True, actor=ARMER)

        assert next_preview(machine) == second
