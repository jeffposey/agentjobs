"""A run gives its slot back when its task closes, not when its process exits (task-482).

The case these are written from is ``run_b400b920``, 2026-09-19: the scripted finish
merged the branch, closed the task and exited; fifty minutes later the dashboard showed
the task Completed and the run ``running``, holding one of three slots, because the
session was still in an exchange with the person who dispatched it.

So the assertions come in pairs. Every test that says the slot is free also says the run
is still live -- releasing a slot by ending the run would pass the first half of this
file and destroy the thing the session was still being used for.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import yaml

from agentjobs.dispatch.ledger import (
    HEALTH_WORK_DONE,
    RunRecord,
    live_runs,
    read_run,
    run_health,
)
from agentjobs.dispatch.runner import runs_root
from agentjobs.dispatch.slots import (
    TASK_CLOSED,
    release_slot,
    release_slot_for_task,
    sweep_released_slots,
)
from agentjobs.execution.factory import execution_store_for
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Lifecycle, Outcome
from agentjobs.projects import Project
from support import task_store


def make_run(
    home: Path,
    run_id: str,
    *,
    task_id: str,
    project_id: str = "demo",
    mode: str = "session",
    status: str = "running",
    journal: bool = True,
) -> RunRecord:
    """A live run on disk, and its journal row, the way a dispatch leaves them."""
    directory = runs_root(home) / run_id
    directory.mkdir(parents=True, exist_ok=True)
    meta: Dict[str, Any] = {
        "run_id": run_id,
        "task_id": task_id,
        "project_id": project_id,
        "mode": mode,
        "status": status,
        "session_id": "0123abcd",
        "started_at": "2026-09-19T16:00:00+00:00",
    }
    (directory / "meta.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")
    if journal:
        store = execution_store_for(home)
        store.admit(
            project_id=project_id,
            task_id=task_id,
            run_id=run_id,
            capacity=None,
            takes_slot=mode not in ("interactive", "walk"),
            mode=mode,
        )
        store.mark_launched(run_id, session_id="0123abcd")
    return read_run(directory)


def closed_task(manager: TaskManager, title: str = "The work") -> Any:
    task = manager.create_task(
        title=title,
        category="infrastructure",
        summary="Something to finish.",
        description="Do the thing.",
        lifecycle=Lifecycle.READY,
    )
    manager.claim_task(task.id, agent="claude")
    manager.close_task(task.id, actor="claude", outcome=Outcome.COMPLETED, body="Merged.")
    return manager.get_task(task.id)


def open_task(manager: TaskManager, title: str = "Still going") -> Any:
    task = manager.create_task(
        title=title,
        category="infrastructure",
        summary="Not finished.",
        description="Do the thing.",
        lifecycle=Lifecycle.READY,
    )
    manager.claim_task(task.id, agent="claude")
    return manager.get_task(task.id)


def world(tmp_path: Path) -> Dict[str, Any]:
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "clone"
    root.mkdir()
    manager = TaskManager(task_store(root / "tasks", project_id="demo"))
    return {
        "home": home,
        "manager": manager,
        "project": Project(id="demo", name="Demo", root=root),
    }


def attempt_for(home: Path, run_id: str) -> Any:
    return execution_store_for(home).attempt(run_id)


# ----- releasing one run ------------------------------------------------------


class TestReleasingOneRun:
    def test_the_run_stops_holding_a_slot_and_stays_live(self, tmp_path: Path) -> None:
        """Both halves of the fix, in one assertion each. The second is the harder one."""
        home = tmp_path / "home"
        home.mkdir()
        record = make_run(home, "run_one", task_id="task-001")
        assert record.takes_slot

        assert release_slot(home, record) is True

        after = read_run(record.path)
        assert after.takes_slot is False
        assert after.slot_released is True
        assert after.slot_released_reason == TASK_CLOSED
        assert after.is_live, "the session is still open and must stay usable"

    def test_every_surface_reads_the_same_release(self, tmp_path: Path) -> None:
        """The reported symptom was two surfaces describing one run differently."""
        home = tmp_path / "home"
        home.mkdir()
        record = make_run(home, "run_two", task_id="task-002")
        release_slot(home, record)
        after = read_run(record.path)

        # The guard's reader, which is what a dispatch counts.
        from agentjobs.dispatch.guards import live_runs as guard_live_runs

        holding = [run for run in guard_live_runs(home) if run.takes_slot]
        assert holding == []
        # The ledger's reader, which is what the API's `occupied` counts.
        assert [run for run in live_runs(home) if run.takes_slot] == []
        # The word every board renders.
        assert run_health(after) == HEALTH_WORK_DONE
        # And the journal, which is what the admitting transaction counts.
        attempt = attempt_for(home, "run_two")
        assert attempt is not None
        assert attempt.holds_slot is False
        assert attempt.takes_slot is True, "what it was admitted as is still recorded"
        assert attempt.is_live

    def test_releasing_twice_keeps_the_first_release(self, tmp_path: Path) -> None:
        """Idempotent, because two callers reach it: the finish and the poller's sweep."""
        home = tmp_path / "home"
        home.mkdir()
        record = make_run(home, "run_three", task_id="task-003")
        assert release_slot(home, record) is True
        first = attempt_for(home, "run_three").slot_released_at

        assert release_slot(home, read_run(record.path)) is False
        assert attempt_for(home, "run_three").slot_released_at == first

    def test_a_run_that_never_held_a_slot_is_left_alone(self, tmp_path: Path) -> None:
        """An interactive session has no slot to give back, and saying it released one
        would put a word on its card that means nothing."""
        home = tmp_path / "home"
        home.mkdir()
        record = make_run(home, "run_chat", task_id="task-004", mode="interactive")

        assert release_slot(home, record) is False
        assert read_run(record.path).slot_released is False

    def test_a_run_with_no_journal_row_still_releases(self, tmp_path: Path) -> None:
        """A run started before the journal is judged from its meta, and so is this."""
        home = tmp_path / "home"
        home.mkdir()
        record = make_run(home, "run_legacy", task_id="task-005", journal=False)

        assert release_slot(home, record) is True
        assert read_run(record.path).takes_slot is False


# ----- what the ceiling counts ------------------------------------------------


class TestTheCeilingStopsCountingIt:
    def test_the_admitting_transaction_lets_the_next_dispatch_in(self, tmp_path: Path) -> None:
        """The primitive, not the advisory check above it.

        ``ExecutionStore.admit`` is what refuses a dispatch inside the transaction that
        allocates the slot, so a fix that freed only the directory scan would have left
        the machine refusing at the last moment with a different message.
        """
        home = tmp_path / "home"
        home.mkdir()
        store = execution_store_for(home)
        record = make_run(home, "run_full", task_id="task-006")

        from agentjobs.execution.errors import CapacityExhausted

        try:
            store.admit(
                project_id="demo", task_id="task-007", run_id="run_next", capacity=1, mode="session"
            )
            raise AssertionError("a full machine must refuse")
        except CapacityExhausted as refusal:
            assert "run_full" in refusal.holders

        release_slot(home, record)

        admitted = store.admit(
            project_id="demo", task_id="task-007", run_id="run_next", capacity=1, mode="session"
        )
        assert admitted.run_id == "run_next"
        assert attempt_for(home, "run_full").is_live, "the released run is still live"


# ----- the sweep --------------------------------------------------------------


class TestTheSweep:
    def test_a_closed_task_frees_its_run_and_an_open_one_does_not(self, tmp_path: Path) -> None:
        """One tick, two runs, and only the finished one gives anything back."""
        state = world(tmp_path)
        home: Path = state["home"]
        manager: TaskManager = state["manager"]
        done = closed_task(manager)
        going = open_task(manager)
        finished = make_run(home, "run_done", task_id=done.id)
        working = make_run(home, "run_going", task_id=going.id)

        released = sweep_released_slots(home, managers={"demo": manager})

        assert [item.run_id for item in released] == ["run_done"]
        assert read_run(finished.path).takes_slot is False
        assert read_run(working.path).takes_slot is True

    def test_the_second_tick_does_nothing(self, tmp_path: Path) -> None:
        """A sweep that reported the same release every ten seconds would be noise."""
        state = world(tmp_path)
        home: Path = state["home"]
        manager: TaskManager = state["manager"]
        done = closed_task(manager)
        make_run(home, "run_done", task_id=done.id)

        assert len(sweep_released_slots(home, managers={"demo": manager})) == 1
        assert sweep_released_slots(home, managers={"demo": manager}) == []

    def test_a_task_that_cannot_be_read_leaves_its_run_alone(self, tmp_path: Path) -> None:
        """Failing to find a record is not evidence the work is over."""
        state = world(tmp_path)
        home: Path = state["home"]
        manager: TaskManager = state["manager"]
        record = make_run(home, "run_orphan", task_id="task-999")

        assert sweep_released_slots(home, managers={"demo": manager}) == []
        assert read_run(record.path).takes_slot is True

    def test_a_project_nothing_can_resolve_leaves_its_run_alone(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        record = make_run(home, "run_stray", task_id="task-010", project_id="gone")

        assert sweep_released_slots(home) == []
        assert read_run(record.path).takes_slot is True


# ----- what the finish calls --------------------------------------------------


class TestReleasingByTask:
    def test_it_finds_the_run_working_that_task(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        mine = make_run(home, "run_mine", task_id="task-011")
        theirs = make_run(home, "run_theirs", task_id="task-012")

        assert release_slot_for_task(home, project_id="demo", task_id="task-011") == ["run_mine"]
        assert read_run(mine.path).takes_slot is False
        assert read_run(theirs.path).takes_slot is True

    def test_a_task_with_no_run_is_not_an_error(self, tmp_path: Path) -> None:
        """The finish calls this after closing a task it may have finished by hand."""
        home = tmp_path / "home"
        home.mkdir()

        assert release_slot_for_task(home, project_id="demo", task_id="task-013") == []

    def test_another_projects_task_of_the_same_id_is_not_touched(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        other = make_run(home, "run_other", task_id="task-014", project_id="elsewhere")

        assert release_slot_for_task(home, project_id="demo", task_id="task-014") == []
        assert read_run(other.path).takes_slot is True
