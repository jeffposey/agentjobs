"""The machine's dispatch queue, against production dispatch (task-459).

Nothing here stubs a gate. Every start goes through ``guards.dispatch_task`` -- the real
guard chain, the real runner, the real journal -- driven by the real
``Controller.tick()``, against the ``Machine`` harness task-416 built for exactly this
kind of evidence. The queue's whole claim is that a dispatch started later is judged by
the same gates as one started now, and a test that mocked the gates would be asserting
the claim rather than checking it.

Two things are deliberately *not* faked and are what the acceptance criteria turn on:

* **The restart** (ac-1). The entry is written by one ``ExecutionStore`` object, every
  in-memory handle on the journal is dropped, and a fresh store opened on the same file
  starts it. A queue that lived in a list would pass every other test here.
* **The dirty tree** (ac-2). The project's working tree is genuinely dirtied and
  ``require_clean_tree`` genuinely refuses, at start time, with nobody present.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List

import pytest

from agentjobs.dispatch import queue as dispatch_queue
from agentjobs.dispatch.guards import (
    IF_FULL_QUEUE,
    AlreadyQueuedError,
    ConcurrencyLimitError,
    DispatchQueueFullError,
    DispatchRequest,
)
from agentjobs.dispatch.journal import journal
from agentjobs.dispatch.queue import dispatch_or_queue
from agentjobs.execution.factory import close_execution_stores
from agentjobs.execution.store import QueuedDispatch
from agentjobs.models_v2 import Ball, BallReason, LogEntryType, Outcome
from agentjobs.projects import ProjectRegistry

from test_execution_controller import Machine, machine

__all__ = ["machine"]  # a fixture, imported by name -- the harness is task-416's


def enqueue(
    box: Machine, task_id: str, *, if_full: str = IF_FULL_QUEUE, queued_by: str = "Jeff Posey"
) -> Any:
    """One dispatch through the production path, with the caller's full-machine choice."""
    project = ProjectRegistry(home=box.home).get("sandbox")
    return dispatch_or_queue(
        manager=box.manager,
        project=project,
        project_config=project.load_config(),
        request=DispatchRequest(task_id=task_id, caused_by=box.authorised_by, if_full=if_full),
        home=box.home,
        api_base="http://127.0.0.1:9",
        queued_by=queued_by,
    )


def waiting(box: Machine) -> List[QueuedDispatch]:
    return dispatch_queue.waiting(box.home)


def task_notes(box: Machine, task_id: str) -> List[str]:
    task = box.manager.get_task(task_id)
    assert task is not None
    return [entry.body or "" for entry in task.log if entry.type is LogEntryType.NOTE]


def free_slot(box: Machine, run_id: str) -> None:
    """End the run holding the slot, through the journal that decides whether it holds one.

    ``effective_live_runs`` -- the function the ceiling is counted from -- asks the
    journal whether each run directory is still live and believes that over the
    directory's own meta. So concluding the attempt *is* freeing the slot, in the one
    place the guard reads; a test that edited a meta file would be asserting against a
    source the guard overrules.
    """
    journal(box.home).conclude(run_id, outcome="completed", status="finished", concluded_by="test")


def dirty(box: Machine) -> None:
    """Make the project's tree genuinely uncommitted, and require a clean one."""
    box.configure(project={"require_clean_tree": True})
    (box.root / "scratch.txt").write_text("in-flight work", encoding="utf-8")


# ----- the default is unchanged (ac-3) -----------------------------------------------


class TestDefaultIsRefusal:
    def test_a_full_machine_still_refuses_a_caller_that_did_not_ask_to_wait(
        self, machine: Machine
    ) -> None:
        machine.configure(limits={"max_concurrent_runs": 1})
        machine.dispatch(machine.task())
        second = machine.task()

        with pytest.raises(ConcurrencyLimitError) as refusal:
            enqueue(machine, second, if_full="refuse")

        assert waiting(machine) == []
        assert refusal.value.reason == "concurrency_limit"

    def test_the_refusal_no_longer_argues_against_queueing(self, machine: Machine) -> None:
        machine.configure(limits={"max_concurrent_runs": 1})
        machine.dispatch(machine.task())

        with pytest.raises(ConcurrencyLimitError) as refusal:
            enqueue(machine, machine.task(), if_full="refuse")

        message = str(refusal.value)
        assert "if_full: queue" in message
        assert "Refused rather than queued" not in message
        assert "promise to spend money" not in message

    def test_a_dispatch_request_defaults_to_refusing(self) -> None:
        assert DispatchRequest(task_id="task-1").if_full == "refuse"


# ----- accepted, durable, and started when a slot frees (ac-1) -----------------------


class TestQueueing:
    def test_a_queued_dispatch_starts_nothing(self, machine: Machine) -> None:
        machine.configure(limits={"max_concurrent_runs": 1})
        machine.dispatch(machine.task())
        second = machine.task()

        entry = enqueue(machine, second)

        assert isinstance(entry, QueuedDispatch)
        assert entry.status == "queued"
        assert entry.task_id == second
        assert entry.queued_by == "Jeff Posey"
        assert len(machine.live_sessions()) == 1  # the first run, and nothing else

    def test_the_task_record_says_it_is_waiting(self, machine: Machine) -> None:
        machine.configure(limits={"max_concurrent_runs": 1})
        machine.dispatch(machine.task())
        second = machine.task()

        enqueue(machine, second)

        [note] = [body for body in task_notes(machine, second) if "queued" in body]
        assert "Jeff Posey" in note
        assert "Nothing has started" in note

    def test_it_starts_on_a_later_tick_when_a_slot_frees(self, machine: Machine) -> None:
        machine.configure(limits={"max_concurrent_runs": 1})
        first = machine.task()
        handle = machine.dispatch(first)
        second = machine.task()
        enqueue(machine, second)

        # Still full: the tick offers the slot to nobody.
        machine.tick()
        assert [entry.status for entry in waiting(machine)] == ["queued"]

        free_slot(machine, handle.run_id)
        machine.tick()

        entry = dispatch_queue.find(machine.home, waiting_id(machine, second))
        assert entry is not None and entry.status == "started"
        assert entry.run_id
        assert any(row["name"].endswith(second) for row in machine.live_sessions())

    def test_it_survives_every_process_that_wrote_it(self, machine: Machine) -> None:
        """ac-1's restart: the queue is on disk, not in anybody's memory."""
        machine.configure(limits={"max_concurrent_runs": 1})
        first = machine.task()
        handle = machine.dispatch(first)
        second = machine.task()
        enqueue(machine, second)

        # Every open handle on the journal is dropped, as a server restart drops them.
        close_execution_stores()

        free_slot(machine, handle.run_id)
        close_execution_stores()

        machine.tick()

        entry = dispatch_queue.find(machine.home, waiting_id(machine, second))
        assert entry is not None and entry.status == "started"


# ----- every gate runs at start time (ac-2) -------------------------------------------


class TestGatesRunAtStartTime:
    def test_a_dirty_tree_dequeues_the_entry_and_writes_the_refusal(self, machine: Machine) -> None:
        machine.configure(limits={"max_concurrent_runs": 1})
        first = machine.task()
        handle = machine.dispatch(first)
        second = machine.task()
        entry = enqueue(machine, second)

        # The condition arrives *after* the dispatch was authorised and queued, which is
        # the whole hazard the old design named: nobody is present when it is judged.
        dirty(machine)
        free_slot(machine, handle.run_id)
        machine.tick()

        settled = dispatch_queue.find(machine.home, entry.queue_id)
        assert settled is not None and settled.status == "refused"
        assert "dirty_tree" in settled.detail
        assert waiting(machine) == []
        assert not any(row["name"].endswith(second) for row in machine.live_sessions())

        [refusal] = [body for body in task_notes(machine, second) if "refused" in body]
        assert "dirty_tree" in refusal
        assert "judged at the moment a queued dispatch starts" in refusal

    def test_a_closed_task_dequeues_rather_than_waiting_forever(self, machine: Machine) -> None:
        machine.configure(limits={"max_concurrent_runs": 1})
        first = machine.task()
        handle = machine.dispatch(first)
        second = machine.task()
        entry = enqueue(machine, second)

        machine.manager.close_task(second, actor="Jeff Posey", outcome=Outcome.COMPLETED)
        free_slot(machine, handle.run_id)
        machine.tick()

        settled = dispatch_queue.find(machine.home, entry.queue_id)
        assert settled is not None and settled.status == "refused"
        assert "task_closed" in settled.detail

    def test_a_transient_refusal_keeps_the_entry_in_line(self, machine: Machine) -> None:
        """A hold is lifted by a person, so the entry waits rather than being thrown away."""
        machine.configure(limits={"max_concurrent_runs": 1})
        first = machine.task()
        handle = machine.dispatch(first)
        second = machine.task()
        entry = enqueue(machine, second)

        # The hold arrives after the dispatch was queued. It clears when somebody lifts
        # it, which is exactly the difference between waiting and being refused.
        machine.manager.handoff(
            second,
            actor="Jeff Posey",
            ball=Ball.AGENT,
            ball_reason=BallReason.HOLD,
            ball_prompt="Not until the design lands.",
        )
        free_slot(machine, handle.run_id)
        lines = machine.tick()

        still = dispatch_queue.find(machine.home, entry.queue_id)
        assert still is not None and still.status == "queued", lines
        assert "task_on_hold" in still.detail
        assert not any(row["name"].endswith(second) for row in machine.live_sessions())


# ----- the bound, and one entry per task (ac-5) ---------------------------------------


class TestBounds:
    def test_the_queue_length_cap_refuses_with_its_own_reason(self, machine: Machine) -> None:
        machine.configure(limits={"max_concurrent_runs": 1, "dispatch_queue_limit": 2})
        machine.dispatch(machine.task())
        enqueue(machine, machine.task())
        enqueue(machine, machine.task())

        with pytest.raises(DispatchQueueFullError) as refusal:
            enqueue(machine, machine.task())

        assert refusal.value.reason == "dispatch_queue_full"
        assert "dispatch_queue_limit" in str(refusal.value)
        assert len(waiting(machine)) == 2

    def test_a_task_cannot_be_queued_twice(self, machine: Machine) -> None:
        machine.configure(limits={"max_concurrent_runs": 1})
        machine.dispatch(machine.task())
        second = machine.task()
        enqueue(machine, second)

        with pytest.raises(AlreadyQueuedError) as refusal:
            enqueue(machine, second)

        assert refusal.value.reason == "already_queued"
        assert len(waiting(machine)) == 1

    def test_the_shipped_default_is_twenty_with_its_reasoning_beside_it(self) -> None:
        """ac-5: the number is recorded, and so is why it is that number."""
        from agentjobs.dispatch import config as dispatch_config
        from agentjobs.dispatch.config import DispatchLimits

        assert DispatchLimits().dispatch_queue_limit == 20
        source = Path(dispatch_config.__file__).read_text(encoding="utf-8")
        clause = source.split("dispatch_queue_limit: int = 20", 1)[1][:2000]
        assert "Rejected" in clause and "unbounded" in clause

    def test_a_queued_start_is_counted_by_the_hourly_cap(self, machine: Machine) -> None:
        """Not exempt: it is a takeoff, and it is counted when it takes off."""
        machine.configure(limits={"max_concurrent_runs": 1, "dispatches_per_hour": 1})
        first = machine.task()
        handle = machine.dispatch(first)
        second = machine.task()
        entry = enqueue(machine, second)

        free_slot(machine, handle.run_id)
        lines = machine.tick()

        still = dispatch_queue.find(machine.home, entry.queue_id)
        assert still is not None
        # Held rather than thrown away: the hour rolls forward on its own. The cap is
        # reported once for the machine rather than written onto every waiting task.
        assert still.status == "queued"
        assert any("last hour" in line or "dispatches_per_hour" in line for line in lines), lines
        assert not any(row["name"].endswith(second) for row in machine.live_sessions())


# ----- order, and what a blocked entry does to the rest -------------------------------


class TestOrder:
    def test_order_is_arrival(self, machine: Machine) -> None:
        machine.configure(limits={"max_concurrent_runs": 1})
        machine.dispatch(machine.task())
        second = machine.task()
        third = machine.task()
        enqueue(machine, second)
        enqueue(machine, third)

        assert [entry.task_id for entry in waiting(machine)] == [second, third]
        assert dispatch_queue.position(machine.home, waiting_id(machine, third)) == 2

    def test_one_stuck_entry_does_not_starve_the_ones_behind_it(self, machine: Machine) -> None:
        machine.configure(limits={"max_concurrent_runs": 1})
        first = machine.task()
        handle = machine.dispatch(first)
        second = machine.task()
        third = machine.task()
        blocked = enqueue(machine, second)
        behind = enqueue(machine, third)
        # The head of the queue is put on hold after it was queued: transient, so it
        # keeps its place, and the entry behind it must not wait on that.
        machine.manager.handoff(
            second,
            actor="Jeff Posey",
            ball=Ball.AGENT,
            ball_reason=BallReason.HOLD,
            ball_prompt="Not yet.",
        )
        free_slot(machine, handle.run_id)

        lines = machine.tick()

        started = dispatch_queue.find(machine.home, behind.queue_id)
        assert started is not None and started.status == "started", lines
        held = dispatch_queue.find(machine.home, blocked.queue_id)
        assert held is not None and held.status == "queued"


# ----- cancelling ----------------------------------------------------------------------


class TestCancel:
    def test_cancelling_removes_it_and_says_so_on_the_task(self, machine: Machine) -> None:
        machine.configure(limits={"max_concurrent_runs": 1})
        machine.dispatch(machine.task())
        second = machine.task()
        entry = enqueue(machine, second)

        removed = dispatch_queue.cancel(
            machine.home, entry.queue_id, requester="Jeff Posey", manager=machine.manager
        )

        assert removed is not None and removed.status == "cancelled"
        assert waiting(machine) == []
        assert any("cancelled" in body for body in task_notes(machine, second))

    def test_cancelling_something_that_already_started_answers_none(self, machine: Machine) -> None:
        machine.configure(limits={"max_concurrent_runs": 1})
        first = machine.task()
        handle = machine.dispatch(first)
        second = machine.task()
        entry = enqueue(machine, second)

        free_slot(machine, handle.run_id)
        machine.tick()

        assert dispatch_queue.cancel(machine.home, entry.queue_id, requester="Jeff") is None

    def test_a_cancelled_entry_never_starts(self, machine: Machine) -> None:
        machine.configure(limits={"max_concurrent_runs": 1})
        first = machine.task()
        handle = machine.dispatch(first)
        second = machine.task()
        entry = enqueue(machine, second)
        dispatch_queue.cancel(machine.home, entry.queue_id, requester="Jeff")

        free_slot(machine, handle.run_id)
        machine.tick()

        assert not any(row["name"].endswith(second) for row in machine.live_sessions())


# ----- the request survives the round trip ----------------------------------------------


def test_the_stored_ask_rebuilds_into_the_same_request(machine: Machine) -> None:
    """What is stored is what was *asked for*, and it comes back unchanged."""
    machine.configure(limits={"max_concurrent_runs": 1})
    machine.dispatch(machine.task())
    second = machine.task()
    entry = enqueue(machine, second)

    rebuilt = dispatch_queue.rebuild_request(entry)

    assert rebuilt.task_id == second
    assert rebuilt.caused_by == machine.authorised_by
    # Never carried across: a start on a full machine means keep waiting, which is the
    # queue's job rather than a field on the request.
    assert rebuilt.if_full == "refuse"
    # Stable, so a start that commits an admission and loses the answer finds it again.
    assert rebuilt.admission_operation_id == f"queue:{entry.queue_id}"


def test_nothing_is_queued_by_a_gate_other_than_the_ceiling(machine: Machine) -> None:
    """A dirty tree with a *free* machine is still a refusal, not a wait."""
    machine.configure(limits={"max_concurrent_runs": 3})
    dirty(machine)

    with pytest.raises(Exception) as refusal:
        enqueue(machine, machine.task())

    assert getattr(refusal.value, "reason", "") == "dirty_tree"
    assert waiting(machine) == []


def waiting_id(box: Machine, task_id: str) -> str:
    """The queue id of the entry for this task, whatever its status."""
    store = journal(box.home)
    for entry in store.queued_dispatches(waiting_only=False):
        if entry.task_id == task_id:
            return entry.queue_id
    raise AssertionError(f"no queue entry for {task_id}")
