"""A task whose branch is being merged reads Finishing, on every surface (task-509).

The task page has shown a running finish since task-321, through a query of its own. The
task *record* did not, and could not: approving a task hands the ball to ``agent``/``work``
and it stays there for the three to four minutes of rebase, gate and merge, so
``display_status`` read "In progress" throughout and every list, board, count and filter
showed a task mid-merge as an ordinary agent-held one.

**Nothing here writes the label onto a record, and nothing may.** A finish moves no axis,
which is what ``TestTheRecordIsNotTouched`` checks from the other end. The label is
derived on read from the finish's own files, by the same function the task page's panel
is built from -- so the two cannot disagree, which is the property
``TestTheChipAndThePanelAgree`` asserts directly rather than by inspection.

The finish directories and lock files here are the shapes ``dispatch.finish`` writes, not
the finish itself: running a real one would mean a real rebase, a real gate and a real
merge inside a unit test. What is *not* faked is the liveness rule -- every case below
goes through ``read_finish_status``, so a test passes only if the real reader agrees.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.dispatch import finish as finish_module
from agentjobs.dispatch.finish_status import (
    live_finishes,
    read_finish_status,
)
from agentjobs.dispatch.ledger import KIND_DISPATCH, KIND_FINISH, locks_root
from agentjobs.models_v2 import (
    Ball,
    BallReason,
    Lifecycle,
    LiveFinishState,
    Outcome,
    QueuedDispatchState,
    Task,
    derived_display_status,
)

from test_execution_controller import Machine, machine

__all__ = ["machine"]  # a fixture, imported by name -- the harness is task-416's

DEAD_PID = 999_999
"""A pid nothing can be running under. Windows and POSIX both report it absent."""


# ----- the shapes a finish leaves on disk ---------------------------------------------


def write_finish(
    home: Path,
    finish_id: str,
    *,
    task_id: str,
    project_id: str = "sandbox",
    outcome: str = "running",
    started_at: Optional[str] = None,
    **meta: Any,
) -> Path:
    """A finish directory as ``FinishDirectory`` would have left it."""
    directory = home / "finishes" / finish_id
    directory.mkdir(parents=True, exist_ok=True)
    payload: Dict[str, Any] = {
        "finish_id": finish_id,
        "task_id": task_id,
        "project_id": project_id,
        "outcome": outcome,
        "started_at": started_at or datetime.now(timezone.utc).isoformat(),
    }
    payload.update(meta)
    (directory / "meta.yaml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return directory


def record(directory: Path, kind: str, **fields: Any) -> None:
    line = json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, **fields})
    with (directory / "phases.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def hold_lock(
    home: Path,
    task_id: str,
    *,
    pid: int,
    kind: str = KIND_FINISH,
    finish_id: str = "",
    project_id: str = "sandbox",
) -> None:
    """The task lock a finish holds for the whole of its attempt.

    Scoped ``project~task``, as ``lock_name`` writes it since task-264, because the
    batched lookup reads the project out of the lock's name to decide whose finish it is.
    """
    directory = locks_root(home)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{project_id}~{task_id}.lock").write_text(
        f"pid={pid} run=run_x kind={kind} finish={finish_id} started=2026-01-01T00:00:00+00:00",
        encoding="utf-8",
    )


def a_live_finish(box: Machine, task_id: str, *, finish_id: str = "fin_live", **meta: Any) -> Path:
    """A finish three steps in, with a working process holding the task's lock.

    The lock names the finish, which is what an approved finish does a moment after
    creating its directory (``RunLock.adopt_finish``), and is the cheap path the batched
    lookup takes.
    """
    directory = write_finish(box.home, finish_id, task_id=task_id, **meta)
    record(directory, "finish_preflight", branch=f"feat/{task_id}-x", worktree=str(box.tmp / "w"))
    record(directory, "finish_step", step="preflight", ok=True, seconds=1.4)
    record(directory, "finish_step", step="runway", ok=True, seconds=0.2)
    record(directory, "finish_step", step="rebase", ok=True, seconds=3.0)
    hold_lock(box.home, task_id, pid=os.getpid(), finish_id=finish_id)
    return directory


def a_working_agent(box: Machine, task_id: str) -> None:
    """A task an agent genuinely holds: claimed, and its run lock held by a live process.

    The comparison the whole task is about. It is deliberately indistinguishable from a
    finishing task on every axis of the record, which is why the label had nothing to
    say before this.
    """
    box.manager.claim_task(task_id, agent="claude")
    hold_lock(box.home, task_id, pid=os.getpid(), kind=KIND_DISPATCH)


@pytest.fixture()
def served(machine: Machine) -> Iterator[TestClient]:
    """The API over the same machine the finishes are written to.

    ``reset_dependency_cache`` at both ends because the project registry and the store
    handles are process-level caches, and this fixture points them at a temporary home.
    """
    reset_dependency_cache()
    with TestClient(app) as client:
        yield client
    reset_dependency_cache()


def detail(client: TestClient, task_id: str) -> Dict[str, Any]:
    """One task's row, through the route the React task page reads."""
    response = client.get(f"/api/projects/sandbox/tasks/{task_id}/detail")
    assert response.status_code == 200, response.text
    body: Dict[str, Any] = response.json()["task"]
    return body


def listing(client: TestClient) -> Dict[str, Dict[str, Any]]:
    """Every listing row, by task id -- the surface the defect was reported against."""
    response = client.get("/api/projects/sandbox/tasks")
    assert response.status_code == 200, response.text
    return {row["id"]: row for row in response.json()}


# ----- the label (a1, a2) -------------------------------------------------------------


class TestTheLabel:
    def test_a_finishing_task_reads_finishing_and_a_worked_one_still_reads_working(
        self, machine: Machine, served: TestClient
    ) -> None:
        """The two states side by side, which is the comparison the report was about.

        Both tasks are ``active``/``agent``/``work`` with a live process holding their
        lock. Before this they rendered the same sentence.
        """
        finishing = machine.task()
        machine.manager.claim_task(finishing, agent="claude")
        a_live_finish(machine, finishing)
        working = machine.task()
        a_working_agent(machine, working)

        rows = listing(served)

        assert rows[finishing]["display_status"] == "Landing"
        assert rows[working]["display_status"] == "Working"

    def test_the_listing_row_carries_the_structure_and_not_only_the_word(
        self, machine: Machine, served: TestClient
    ) -> None:
        """A client filters and disables buttons on this; matching on prose is the
        failure ENGINEERING.md's rendered-value rule exists to prevent."""
        task_id = machine.task()
        machine.manager.claim_task(task_id, agent="claude")
        a_live_finish(machine, task_id)

        row = listing(served)[task_id]

        assert row["live_finish"]["finish_id"] == "fin_live"
        assert row["live_finish"]["state"] == "running"
        assert row["live_finish"]["branch"] == f"feat/{task_id}-x"
        # The step after the last one recorded, with the sentence a reader who has never
        # opened ENGINEERING.md needs for it.
        assert row["live_finish"]["current_step"] == "gate"
        assert row["live_finish"]["step_meaning"] == "Running the full gate on the rebased branch"

    def test_the_task_page_says_the_same_thing_as_the_list(
        self, machine: Machine, served: TestClient
    ) -> None:
        """Two read models, one derivation. A surface that disagreed would be the defect
        in a new place."""
        task_id = machine.task()
        machine.manager.claim_task(task_id, agent="claude")
        a_live_finish(machine, task_id)

        page = detail(served, task_id)

        assert page["display_status"] == "Landing"
        assert page["live_finish"]["finish_id"] == "fin_live"

    def test_a_task_with_no_finish_carries_nothing(
        self, machine: Machine, served: TestClient
    ) -> None:
        task_id = machine.task()

        row = listing(served)[task_id]

        assert row["live_finish"] is None
        assert row["display_status"] == "Ready"


# ----- only a live finish (a3) --------------------------------------------------------


class TestOnlyALiveFinishGetsTheLabel:
    @pytest.mark.parametrize("outcome", ["finished", "escalated", "declined"])
    def test_a_finish_that_ended_does_not_read_finishing(
        self, machine: Machine, served: TestClient, outcome: str
    ) -> None:
        """ "Finishing" means live. A finish that merged an hour ago is the task's own
        closed state, and the task page's panel already tells that story."""
        task_id = machine.task()
        machine.manager.claim_task(task_id, agent="claude")
        a_live_finish(
            machine,
            task_id,
            outcome=outcome,
            finished_at=datetime.now(timezone.utc).isoformat(),
        )

        row = listing(served)[task_id]

        assert row["live_finish"] is None
        assert row["display_status"] == "Working"

    def test_a_finish_whose_process_is_gone_does_not_read_finishing(
        self, machine: Machine, served: TestClient
    ) -> None:
        """An interrupted finish -- the machine rebooted mid-gate -- wrote no ending at
        all. Reading it as live is exactly the spinner that never stops, and the lock's
        own staleness rule is what tells the two apart."""
        task_id = machine.task()
        machine.manager.claim_task(task_id, agent="claude")
        a_live_finish(machine, task_id)
        hold_lock(machine.home, task_id, pid=DEAD_PID, finish_id="fin_live")

        row = listing(served)[task_id]

        assert row["live_finish"] is None
        interrupted = read_finish_status(machine.home, task_id, "sandbox")
        assert interrupted is not None and interrupted.state == "interrupted"

    def test_a_closed_task_keeps_its_outcome_while_the_finish_cleans_up(
        self, machine: Machine, served: TestClient
    ) -> None:
        """The finish closes the task at its ``close`` step and then spends a second or
        two stopping the worktree's processes, removing it and deleting the branch. "Completed" is the useful
        truth in that window -- the merge has landed, which is the whole question -- and
        "Finishing" would replace an answer with a process."""
        task_id = machine.task()
        machine.manager.claim_task(task_id, agent="claude")
        directory = a_live_finish(machine, task_id)
        for step in ("gate", "merge", "rebuild", "restart", "verify", "close"):
            record(directory, "finish_step", step=step, ok=True, seconds=1.0)
        machine.manager.close_task(task_id, actor="claude", outcome=Outcome.COMPLETED)

        row = listing(served)[task_id]

        assert row["display_status"] == "Completed"
        # The structure is still carried: a surface with room to draw the last steps
        # may, it just does not get to overwrite the sentence.
        assert row["live_finish"]["current_step"] == "teardown"

    def test_a_closed_task_with_a_finish_that_closed_nothing_reads_completed(
        self, machine: Machine, served: TestClient
    ) -> None:
        """task-514: the duplicate `fin_76cf6a4e`, on the surface this label lives on.

        One field apart from the case above -- this finish recorded no ``close`` step and
        merged nothing, so it cannot be the finish that closed the task. It is the second
        finish for a task the first one already finished, and a row reading "Finishing"
        beside a page reading "Completed" is exactly what nobody could make sense of.
        """
        task_id = machine.task()
        machine.manager.claim_task(task_id, agent="claude")
        a_live_finish(machine, task_id)
        machine.manager.close_task(task_id, actor="claude", outcome=Outcome.COMPLETED)

        row = listing(served)[task_id]

        assert row["live_finish"] is None
        assert row["display_status"] == "Completed"
        overtaken = read_finish_status(machine.home, task_id, "sandbox", task_open=False)
        assert overtaken is not None and overtaken.state == "overtaken"


# ----- the axes are untouched (a4) ----------------------------------------------------


class TestTheRecordIsNotTouched:
    def test_the_state_axes_are_the_same_before_and_during_a_finish(
        self, machine: Machine, served: TestClient
    ) -> None:
        """The whole argument for deriving this. A finish is not a holder of the ball,
        and the axes move only through the manager verbs."""
        task_id = machine.task()
        machine.manager.claim_task(task_id, agent="claude")
        before = detail(served, task_id)
        a_live_finish(machine, task_id)

        during = detail(served, task_id)

        axes = ("lifecycle", "ball", "ball_reason")
        assert [during[key] for key in axes] == [before[key] for key in axes]
        assert (during["lifecycle"], during["ball"], during["ball_reason"]) == (
            "active",
            "agent",
            "work",
        )

    def test_no_axis_carries_a_finishing_value(self) -> None:
        """A new ``BallReason`` was the alternative, and this is what rules it out from
        the enum's own side rather than from a reviewer's memory."""
        values = (
            {member.value for member in Lifecycle}
            | {member.value for member in Ball}
            | {member.value for member in BallReason}
            | {member.value for member in Outcome}
        )

        assert not {value for value in values if "finish" in value}


# ----- how the label is composed ------------------------------------------------------


class TestTheDerivation:
    """:func:`derived_display_status` directly, where the ordering rules are decided."""

    def working(self, machine: Machine) -> Task:
        task_id = machine.task()
        machine.manager.claim_task(task_id, agent="claude")
        task = machine.manager.get_task(task_id)
        assert task is not None
        return task

    def test_finishing_replaces_the_label_an_open_task_would_otherwise_have(
        self, machine: Machine
    ) -> None:
        task = self.working(machine)

        assert derived_display_status(task, None, None) == "Working"
        assert derived_display_status(task, None, LiveFinishState(state="running")) == "Landing"

    def test_finishing_outranks_a_queued_dispatch(self, machine: Machine) -> None:
        """Both are facts only a read surface can see, so which wins is decided in one
        place. A finish holds the task's run lock, so nothing the queue promises can
        happen until it ends."""
        task = self.working(machine)
        queued = QueuedDispatchState(queue_id="q1", position=1, queued_at="2026-09-20T00:00:00Z")

        assert derived_display_status(task, queued, LiveFinishState(state="running")) == "Landing"

    def test_a_closed_task_keeps_its_outcome(self, machine: Machine) -> None:
        task_id = machine.task()
        machine.manager.claim_task(task_id, agent="claude")
        machine.manager.close_task(task_id, actor="claude", outcome=Outcome.COMPLETED)
        task = machine.manager.get_task(task_id)
        assert task is not None

        assert derived_display_status(task, None, LiveFinishState(state="running")) == "Completed"


# ----- the batched lookup agrees with the per-task one --------------------------------


class TestTheChipAndThePanelAgree:
    """The constraint the task set: the label must come from the same evidence the task
    page's ``FinishPanel`` shows. Asserted by comparing the two readers, not by reading
    the code and believing they match."""

    def test_the_batch_and_the_per_task_read_report_the_same_attempt(
        self, machine: Machine
    ) -> None:
        task_id = machine.task()
        a_live_finish(machine, task_id)

        batched = live_finishes(machine.home, "sandbox")[task_id]
        per_task = read_finish_status(machine.home, task_id, "sandbox")

        assert per_task is not None
        assert (batched.finish_id, batched.state, batched.live, batched.current_step) == (
            per_task.finish_id,
            per_task.state,
            per_task.live,
            per_task.current_step,
        )

    def test_a_run_finishing_itself_is_found_although_its_lock_says_run(
        self, machine: Machine
    ) -> None:
        """The ``--automerge-release`` shape, and the reason the batch does not filter on
        ``holder.is_finish``.

        A run finishing its own work keeps the ``kind=run`` lock it already holds and
        never adopts a finish one, so a candidate scan trusting the lock's kind would
        miss precisely the case an autonomous project merges through.
        """
        task_id = machine.task()
        directory = write_finish(machine.home, "fin_own", task_id=task_id, run_id="run_x")
        record(directory, "finish_step", step="preflight", ok=True, seconds=1.0)
        hold_lock(machine.home, task_id, pid=os.getpid(), kind=KIND_DISPATCH)

        assert live_finishes(machine.home, "sandbox")[task_id].state == "running"

    def test_an_agent_merely_working_a_task_is_not_reported_as_finishing(
        self, machine: Machine
    ) -> None:
        """The other half of the rule above: a ``kind=run`` lock makes a task a
        *candidate*, and the confirmation is what drops it."""
        task_id = machine.task()
        a_working_agent(machine, task_id)

        assert live_finishes(machine.home, "sandbox") == {}

    def test_the_starting_window_is_covered_before_any_directory_exists(
        self, machine: Machine
    ) -> None:
        """One to two seconds between the click and the finish's first write, with no
        lock and no directory. A list that said nothing here would show "In progress" on
        the task somebody had just pressed Approve on."""
        task_id = machine.task()
        finish_module.write_spawn_marker(
            machine.home, task_id, project_id="sandbox", approver="Jeff Posey"
        )

        assert live_finishes(machine.home, "sandbox")[task_id].state == "starting"

    def test_a_marker_nothing_ever_picked_up_stops_being_a_candidate(
        self, machine: Machine
    ) -> None:
        """A spawn that never started leaves its marker behind forever. Beyond the
        grace, ``_starting`` calls it interrupted, and the scan stops asking about it."""
        task_id = machine.task()
        finish_module.write_spawn_marker(
            machine.home, task_id, project_id="sandbox", approver="Jeff Posey"
        )
        stale = datetime.now(timezone.utc) - timedelta(hours=3)
        os.utime(
            finish_module.spawn_marker_path(machine.home, task_id),
            (stale.timestamp(), stale.timestamp()),
        )

        assert live_finishes(machine.home, "sandbox") == {}

    def test_another_project_s_finish_is_not_this_project_s(self, machine: Machine) -> None:
        task_id = machine.task()
        write_finish(machine.home, "fin_other", task_id=task_id, project_id="elsewhere")
        hold_lock(machine.home, task_id, pid=os.getpid(), finish_id="fin_other", project_id="other")

        assert live_finishes(machine.home, "sandbox") == {}


# ----- what it costs (a6) -------------------------------------------------------------


class TestWhatItCosts:
    """The cost is bounded by the machine's live locks, never by the number of rows.

    Asserted as *work done* rather than as elapsed time, for the reason
    ``tests/test_performance_budgets.py`` opens with: a wall-clock threshold means
    something different on every machine, drifts with hardware, and gets loosened until
    it catches nothing. "This request read the finishes once" means the same thing
    everywhere, forever, and it is exactly the property a naive implementation would
    violate while still returning the right answer -- just 500 times more slowly.

    The measured wall-clock figures, and what the per-task alternative cost beside them,
    are in :func:`agentjobs.dispatch.finish_status.live_finishes`.
    """

    def counting(self, monkeypatch: pytest.MonkeyPatch) -> Dict[str, int]:
        """Count every finish-directory metadata read and every scan of the finishes."""
        from agentjobs.dispatch import finish_status

        counts = {"meta": 0, "scans": 0}
        real_meta = finish_status.read_meta
        real_batch = finish_status.live_finishes

        def meta(directory: Path) -> Dict[str, Any]:
            counts["meta"] += 1
            return real_meta(directory)

        def batch(home: Path, project_id: str = "") -> Any:
            counts["scans"] += 1
            return real_batch(home, project_id)

        monkeypatch.setattr(finish_status, "read_meta", meta)
        monkeypatch.setattr(finish_status, "live_finishes", batch)
        return counts

    def test_a_long_list_costs_what_a_short_one_costs(
        self, machine: Machine, served: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The assertion the whole batched lookup exists for.

        One finish is live and forty tasks are listed. A per-row lookup would resolve the
        newest finish directory forty times; this resolves it once, so the reads a
        request makes are the same number they were when the list held one row.
        """
        finishing = machine.task()
        machine.manager.claim_task(finishing, agent="claude")
        a_live_finish(machine, finishing)

        counts = self.counting(monkeypatch)
        short = listing(served)
        one_row = dict(counts)
        for _ in range(39):
            machine.task()
        counts["meta"] = counts["scans"] = 0
        long = listing(served)

        assert len(short) == 1 and len(long) == 40
        assert long[finishing]["display_status"] == "Landing"
        assert counts["scans"] == 1, "the finishes are read once per request, not once per row"
        # Without this the comparison below would pass on two zeroes -- which is what it
        # would read if the label stopped being derived at all.
        assert one_row["meta"] > 0, "no finish directory was read for a task that is finishing"
        assert counts["meta"] == one_row["meta"], (
            "reading the finishes cost more for forty rows than for one, so the lookup "
            "is scaling with the corpus -- which is the defect this batch replaced"
        )

    def test_a_request_pays_nothing_when_nothing_holds_a_lock(
        self, machine: Machine, served: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ordinary case, and the reason the candidate scan comes first.

        No lock in this project means no candidate, which means nothing to resolve a
        directory *for* -- so the expensive half never runs at all, however many finishes
        the machine has accumulated.
        """
        machine.task()
        write_finish(machine.home, "fin_old", task_id="task-999", outcome="finished")

        counts = self.counting(monkeypatch)
        listing(served)

        assert counts["scans"] == 1
        assert counts["meta"] == 0, "a finish directory was read with no candidate to read it for"
