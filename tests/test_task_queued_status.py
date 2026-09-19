"""A task whose dispatch is waiting for a slot reads Queued, on every surface (task-476).

The slot board has shown a waiting dispatch since task-459. The task itself did not: its
record read ``Ready``, so the page a person actually opens said nothing was happening to
a task the machine had already promised to start.

**Nothing here writes the label onto a record, and nothing may.** A queued dispatch
deliberately does not claim the task -- every dispatch gate is judged when a slot frees --
so the state axes genuinely stay ``ready``/``agent``/``available`` throughout, which is
what ``TestTheRecordIsNotTouched`` checks from the other end.

The queue entries are real ones, written by ``dispatch_or_queue`` onto a genuinely full
machine, and the refusal in ``TestAnEntryThatLeavesTheQueue`` is a real gate refusing a
real dirty tree under ``Controller.tick()``. A test that inserted a row into the queue
table would be asserting the shape of this feature's own fixture.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator, List, Set
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.api.models import TaskRead
from agentjobs.dispatch import queue as dispatch_queue
from agentjobs.dispatch.journal import journal

from test_execution_controller import Machine, machine
from test_start_pause import claude_profile, enqueue, fill_the_machine, seed_incident

__all__ = ["machine"]  # a fixture, imported by name -- the harness is task-416's


@pytest.fixture()
def served(machine: Machine) -> Iterator[TestClient]:
    """The API over the same machine the queue entries are written to.

    ``reset_dependency_cache`` at both ends because the project registry and the store
    handles are process-level caches, and this fixture points them at a temporary home.
    """
    reset_dependency_cache()
    with TestClient(app) as client:
        yield client
    reset_dependency_cache()


def free_slot(box: Machine, run_id: str) -> None:
    """End a run through the journal the ceiling is counted from."""
    journal(box.home).conclude(run_id, outcome="completed", status="finished", concluded_by="test")


def detail(client: TestClient, task_id: str) -> Dict[str, Any]:
    """One task's row, through the route the React task page reads.

    ``GET /tasks/{id}`` is deliberately not it: that route answers with the bare stored
    ``Task`` and has never carried a derived read-model field -- not the dependency
    facts, not ``self_clearing_wait``, and not this. The page, the MCP ``task_get`` and
    the client all read ``/detail``.
    """
    response = client.get(f"/api/projects/sandbox/tasks/{task_id}/detail")
    assert response.status_code == 200, response.text
    body: Dict[str, Any] = response.json()["task"]
    return body


def a_waiting_dispatch(box: Machine) -> tuple[str, str, Any]:
    """A full machine, one task queued behind it, and one task queued behind nothing.

    Returns the queued task, an untouched ready task, and the queue entry.
    """
    fill_the_machine(box)
    waiting_task = box.task()
    entry = enqueue(box, waiting_task)
    idle_task = box.task()
    return waiting_task, idle_task, entry


# ----- the label (ac-1, ac-2) ---------------------------------------------------------


class TestTheLabel:
    def test_a_waiting_dispatch_reads_queued_and_a_task_without_one_still_reads_ready(
        self, machine: Machine, served: TestClient
    ) -> None:
        queued, idle, entry = a_waiting_dispatch(machine)

        waiting_row = detail(served, queued)
        idle_row = detail(served, idle)

        assert waiting_row["display_status"] == "Queued"
        assert waiting_row["queued_dispatch"]["queue_id"] == entry.queue_id
        assert waiting_row["queued_dispatch"]["position"] == 1
        assert waiting_row["queued_dispatch"]["queued_by"] == ""
        # The state axes are untouched, which is the whole argument for deriving this.
        assert (waiting_row["lifecycle"], waiting_row["ball"], waiting_row["ball_reason"]) == (
            "ready",
            "agent",
            "available",
        )
        assert idle_row["display_status"] == "Ready"
        assert idle_row["queued_dispatch"] is None

    def test_the_place_in_line_is_named_when_the_entry_is_not_next(
        self, machine: Machine, served: TestClient
    ) -> None:
        fill_the_machine(machine)
        first = machine.task()
        enqueue(machine, first)
        second = machine.task()
        enqueue(machine, second)

        assert detail(served, first)["display_status"] == "Queued"
        assert detail(served, second)["display_status"] == "Queued (place 2)"
        assert detail(served, second)["queued_dispatch"]["position"] == 2

    def test_an_entry_held_off_by_an_incident_does_not_read_as_next_in_line(
        self, machine: Machine, served: TestClient
    ) -> None:
        """A start paused on a usage limit is not being tried at all (task-463)."""
        queued, _, _ = a_waiting_dispatch(machine)
        incident = seed_incident(machine, claude_profile(machine))

        row = detail(served, queued)

        assert row["display_status"] == "Queued (start paused)"
        assert row["queued_dispatch"]["paused_by"] == incident

    def test_the_label_replaces_ready_rather_than_a_more_urgent_sentence(self) -> None:
        """A task needing a person says so, whatever the queue is doing with it.

        Unit rather than end-to-end because the state it describes is one the dispatch
        guard refuses to create: a queued entry whose task was handed to a human while it
        waited. The queue does not notice, because it judges nothing until a slot frees.
        """
        from agentjobs.models_v2 import QueuedDispatchState, queued_display_status

        entry = QueuedDispatchState(queue_id="q_1", position=1, queued_at="2026-09-19T15:29:00Z")
        ready = _task(ball="agent", ball_reason="available")
        review = _task(ball="human", ball_reason="review", ball_prompt="Look at this.")

        assert queued_display_status(ready, entry) == "Queued"
        assert queued_display_status(review, entry) == "Needs review"
        assert queued_display_status(ready, None) == "Ready"


def _task(**fields: Any) -> Any:
    from datetime import datetime, timezone

    from agentjobs.models_v2 import Task

    moment = datetime.now(timezone.utc)
    return Task.model_validate(
        {
            "id": "task-001",
            "title": "A task",
            "created": moment,
            "updated": moment,
            "category": "general",
            "queue_position": 100,
            "spec": {"summary": "s", "description": "d"},
            "lifecycle": "ready",
            **fields,
        }
    )


# ----- every surface that builds a TaskRead (ac-3) ------------------------------------

#: Every **read** route that answers with a ``TaskRead``, and how to reach one task's row
#: through it. The keys are endpoint function names, which is what
#: ``test_no_read_surface_is_left_out`` walks ``app.routes`` for -- a route added tomorrow
#: that answers with a ``TaskRead`` and is not named here turns this file red rather than
#: quietly becoming the one surface that tells a person a queued task is Ready.
SURFACES = ("list_tasks", "get_task_detail", "get_dashboard", "search_tasks")

#: The module whose one helper builds the ``TaskRead`` every *mutation* answers with.
#: The walk below insists every non-GET route answering with one is served from here, so
#: a second envelope builder somewhere else has to be noticed rather than inherited.
ENVELOPE_MODULE = "agentjobs.api.routes.status"


def rows_from_surface(client: TestClient, surface: str, task_id: str) -> List[Dict[str, Any]]:
    """Every row a surface returns for ``task_id``, however deeply it nests them."""
    if surface == "list_tasks":
        response = client.get("/api/projects/sandbox/tasks")
        found = response.json()
    elif surface == "get_task_detail":
        response = client.get(f"/api/projects/sandbox/tasks/{task_id}/detail")
        body = response.json()
        # The detail carries the task, its parent and its children through three separate
        # constructions. All three are read, because all three are drawn on the page.
        found = [body["task"], *(body["children"] or [])]
        if body.get("parent_task"):
            found.append(body["parent_task"])
    elif surface == "get_dashboard":
        response = client.get("/api/projects/sandbox/dashboard")
        body = response.json()
        found = [row for value in body.values() if isinstance(value, list) for row in value]
        found.append(body.get("next_task"))
    elif surface == "search_tasks":
        response = client.get("/api/projects/sandbox/search", params={"q": "Recoverable"})
        found = response.json()
    else:  # pragma: no cover - the walk below is what stops this being reachable
        raise AssertionError(f"no reader written for {surface}")
    assert response.status_code == 200, response.text
    return [row for row in found if isinstance(row, dict) and row.get("id") == task_id]


class TestEverySurfaceCarriesIt:
    @pytest.mark.parametrize("surface", SURFACES)
    def test_the_surface_reports_the_waiting_entry(
        self, machine: Machine, served: TestClient, surface: str
    ) -> None:
        queued, _, entry = a_waiting_dispatch(machine)

        rows = rows_from_surface(served, surface, queued)

        assert rows, f"{surface} returned no row for the queued task"
        for row in rows:
            assert row["display_status"] == "Queued", surface
            assert row["queued_dispatch"]["queue_id"] == entry.queue_id, surface

    @pytest.mark.parametrize("surface", SURFACES)
    def test_the_surface_still_reads_ready_without_one(
        self, machine: Machine, served: TestClient, surface: str
    ) -> None:
        _, idle, _ = a_waiting_dispatch(machine)

        rows = rows_from_surface(served, surface, idle)

        assert rows, f"{surface} returned no row for the idle task"
        for row in rows:
            assert row["display_status"] == "Ready", surface
            assert row["queued_dispatch"] is None, surface

    def test_the_mutation_envelope_carries_it_too(
        self, machine: Machine, served: TestClient
    ) -> None:
        """The fifth construction site: what a verb hands back after it writes.

        A queued task is claimable by hand while it waits, and the body that comes back
        from the claim is what the page redraws itself from.
        """
        queued, _, entry = a_waiting_dispatch(machine)

        answer = served.post(
            f"/api/projects/sandbox/tasks/{queued}/reprioritize",
            params={"envelope": "true"},
            json={"priority": "high", "actor": "Jeff Posey", "operation_id": str(uuid4())},
        )

        assert answer.status_code == 200, answer.text
        body = answer.json()["task"]
        assert body["queued_dispatch"]["queue_id"] == entry.queue_id
        assert body["display_status"] == "Queued"

    def test_no_read_surface_is_left_out(self) -> None:
        """Walk the application for routes answering with a ``TaskRead``.

        The completeness half, and the reason this is not four hand-written assertions: a
        fifth construction site added later is a surface that lies, and prose asking the
        next author to remember is not a check. The walk is
        ``tests/test_authorization.py``'s, for the same reason it is used there.
        """
        readers: Set[str] = set()
        writers: Set[Any] = set()
        for route in app.routes:
            endpoint = getattr(route, "endpoint", None)
            model = getattr(route, "response_model", None)
            if endpoint is None or model is None or not _mentions_task_read(model):
                continue
            if "GET" in (getattr(route, "methods", None) or set()):
                readers.add(endpoint.__name__)
            else:
                writers.add(endpoint)

        assert readers == set(SURFACES), (
            "a read route answering with a TaskRead is not exercised by this file: "
            f"{sorted(readers.symmetric_difference(SURFACES))}"
        )
        strays = sorted(
            f"{endpoint.__module__}.{endpoint.__name__}"
            for endpoint in writers
            if endpoint.__module__ != ENVELOPE_MODULE
        )
        assert not strays, f"a mutation builds a TaskRead outside {ENVELOPE_MODULE}: {strays}"


def _mentions_task_read(annotation: Any) -> bool:
    """Whether ``TaskRead`` appears anywhere in a response model, however nested."""
    if annotation is TaskRead:
        return True
    if isinstance(annotation, type):
        fields = getattr(annotation, "model_fields", None)
        if fields:
            return any(_mentions_task_read(field.annotation) for field in fields.values())
        return False
    return any(_mentions_task_read(argument) for argument in getattr(annotation, "__args__", ()))


# ----- an entry that leaves the queue (ac-4, ac-5) ------------------------------------


class TestAnEntryThatLeavesTheQueue:
    def test_cancelling_returns_the_task_to_ready(
        self, machine: Machine, served: TestClient
    ) -> None:
        queued, _, entry = a_waiting_dispatch(machine)
        assert detail(served, queued)["display_status"] == "Queued"

        cancelled = served.post(f"/api/projects/sandbox/dispatch/runs/{entry.queue_id}/cancel")
        assert cancelled.status_code == 200, cancelled.text

        row = detail(served, queued)
        assert row["display_status"] == "Ready"
        assert row["queued_dispatch"] is None

    def test_cancelling_moves_no_state_axis(self, machine: Machine, served: TestClient) -> None:
        """The record is where the queue is not, before and after.

        The cancellation writes a note, which task-459 added and this task did not: it is
        the evidence that nothing ran. What must not appear is a lifecycle, ball or
        ball_reason that moved, because nothing about the task changed -- only what the
        machine intended to do to it.
        """
        queued, _, entry = a_waiting_dispatch(machine)
        before = detail(served, queued)

        served.post(f"/api/projects/sandbox/dispatch/runs/{entry.queue_id}/cancel")

        after = detail(served, queued)
        axes = ("lifecycle", "ball", "ball_reason", "outcome", "queue_position", "assignment")
        assert [after[axis] for axis in axes] == [before[axis] for axis in axes]
        added = after["log"][len(before["log"]) :]
        assert [entry_["type"] for entry_ in added] == ["note"]

    def test_an_entry_refused_at_start_reads_ready_again_with_its_refusal_kept(
        self, machine: Machine, served: TestClient
    ) -> None:
        """A gate refusing when the slot frees is the case nobody is present for."""
        machine.configure(limits={"max_concurrent_runs": 1})
        holder = machine.task()
        handle = machine.dispatch(holder)
        queued = machine.task()
        entry = enqueue(machine, queued)
        assert detail(served, queued)["display_status"] == "Queued"

        # The condition arrives after the dispatch was authorised and queued.
        machine.configure(limits={"max_concurrent_runs": 1}, project={"require_clean_tree": True})
        (machine.root / "scratch.txt").write_text("in-flight work", encoding="utf-8")
        free_slot(machine, handle.run_id)
        machine.tick()

        settled = dispatch_queue.find(machine.home, entry.queue_id)
        assert settled is not None and settled.status == "refused"

        row = detail(served, queued)
        assert row["display_status"] == "Ready"
        assert row["queued_dispatch"] is None
        refusals = [
            note["body"]
            for note in row["log"]
            if note["type"] == "note" and "dirty_tree" in (note["body"] or "")
        ]
        assert refusals, "the refusal note was not kept on the record"


# ----- what else reads a status (the spec's step 5) -----------------------------------


class TestTheCommandLine:
    """``agentjobs list`` reads the store directly, so it inherits nothing by itself.

    Verified rather than assumed, which is what the spec asked for: the browser and MCP
    both read the API and got this for free, and this one did not -- it opened the store
    and printed ``Task.display_status``, which is ``Ready`` for a queued task by design.
    """

    def run_list(self, box: Machine) -> str:
        from typer.testing import CliRunner

        from agentjobs.cli import app

        result = CliRunner().invoke(app, ["list"])
        assert result.exit_code == 0, result.output
        return result.output

    def test_it_says_queued_for_a_waiting_dispatch_and_ready_for_the_rest(
        self, machine: Machine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        queued, idle, _ = a_waiting_dispatch(machine)
        monkeypatch.chdir(machine.root)

        lines = {
            line.split(" | ")[0].removeprefix("- "): line
            for line in self.run_list(machine).splitlines()
            if line.startswith("- ")
        }

        assert "[Queued," in lines[queued]
        assert "[Ready," in lines[idle]


# ----- what it costs (the task's cost note) -------------------------------------------


class TestWhatItCosts:
    def test_a_list_of_many_rows_reads_the_queue_once(
        self, machine: Machine, served: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One read per request, shared across the rows, rather than one per row.

        Counted rather than timed: a threshold in milliseconds means a different thing on
        every machine, and the claim being made here is about a count.
        """
        from agentjobs.dispatch import queue as queue_module

        a_waiting_dispatch(machine)
        for _ in range(8):
            machine.task()

        reads: List[Path] = []
        real = queue_module.waiting

        def counted(home: Path) -> Any:
            reads.append(home)
            return real(home)

        monkeypatch.setattr(queue_module, "waiting", counted)

        response = served.get("/api/projects/sandbox/tasks")

        assert response.status_code == 200, response.text
        assert len(response.json()) >= 10
        assert len(reads) == 1

    def test_a_request_that_reads_no_task_reads_no_queue(
        self, machine: Machine, served: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentjobs.dispatch import queue as queue_module

        reads: List[Path] = []

        def counted(home: Path) -> Any:
            reads.append(home)
            return []

        monkeypatch.setattr(queue_module, "waiting", counted)

        assert served.get("/api/version").status_code == 200
        assert reads == []
