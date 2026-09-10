"""The server half of the React queue surface: listing order, and a dashboard that lives.

Task-207, implementing the React row of section 10 of ``docs/task-selection-design.md``.
Two obligations that the browser cannot meet on its own:

* **The order is the server's.** The task list used to sort by ``updated`` descending
  in the browser, so the screen a human read and the queue the scheduler acted on were
  two different orders, and neither had been chosen by anybody. Deleting the client
  sort only helps if what arrives is already ordered, which is what
  :class:`TestListingArrivesInQueueOrder` pins.
* **A corrupt queue must not take the dashboard down.** ``build_dashboard_snapshot``
  called ``get_next_task()`` unguarded, and since task-205 that raises. The dashboard
  is the surface whose job is to *say* the queue is broken, so it is the last one that
  may 500 when it is.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import get_task_manager, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.api.routes.status import get_acting_project
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Lifecycle, LogEntryType, Outcome, Priority
from agentjobs.projects import Project
from support import task_store

CONFIG: Dict[str, object] = {
    "project_name": "Fixture",
    "tasks_directory": "tasks",
    "categories": ["general"],
    "actors": [
        {"name": "Ada", "kind": "human", "display_name": "Ada Lovelace"},
        {"name": "bot", "kind": "agent", "display_name": "Bot"},
    ],
    "default_user": "Ada",
}


@pytest.fixture()
def project(tmp_path: Path) -> Iterator[Tuple[Path, TaskManager]]:
    (tmp_path / ".agentjobs").mkdir(parents=True)
    (tmp_path / ".agentjobs" / "config.yaml").write_text(yaml.safe_dump(CONFIG), encoding="utf-8")
    yield tmp_path, TaskManager(task_store(tmp_path / "tasks"))


@pytest.fixture()
def api(project) -> Iterator[Tuple[TestClient, TaskManager, Path]]:
    root, manager = project
    reset_dependency_cache()
    acting = Project(id="fixture", name="Fixture", root=root)
    app.dependency_overrides[get_task_manager] = lambda: manager
    app.dependency_overrides[get_acting_project] = lambda: acting
    with TestClient(app) as client:
        yield client, manager, root
    app.dependency_overrides.clear()
    reset_dependency_cache()


def make(
    manager: TaskManager,
    task_id: str,
    *,
    priority: Priority = Priority.HIGH,
    lifecycle: Lifecycle = Lifecycle.READY,
    **kwargs: Any,
) -> str:
    """Create one task through the real verb, so it gets a real position."""
    manager.create_task(
        id=task_id,
        title=f"Title of {task_id}",
        description="Body.",
        priority=priority,
        lifecycle=lifecycle,
        actor="bot",
        **kwargs,
    )
    return task_id


def position(manager: TaskManager, task_id: str) -> int:
    task = manager.get_task(task_id)
    assert task is not None
    assert task.queue_position is not None
    return task.queue_position


def break_the_queue(root: Path, task_id: str, *, band: str, at: int) -> None:
    """Force a duplicate position by hand, as a bad merge or a stray editor would."""
    path = root / "tasks" / f"{task_id}.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["queue_position"] = at
    raw["priority"] = band
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


def listed(client: TestClient, **query: str) -> List[str]:
    """The ids the list endpoint returns, in the order it returned them."""
    response = client.get("/api/tasks", params=query)
    assert response.status_code == 200
    return [task["id"] for task in response.json()]


# ---------------------------------------------------------------------------
# sc-1 -- the order is the server's, and it is the queue's
# ---------------------------------------------------------------------------


class TestListingArrivesInQueueOrder:
    """``list_tasks`` answers in ``(band, queue_position)``, whatever disk says."""

    def test_band_then_position_not_creation_or_file_order(self, api) -> None:
        client, manager, _ = api
        make(manager, "task-a", priority=Priority.MEDIUM)
        make(manager, "task-b", priority=Priority.CRITICAL)
        make(manager, "task-c", priority=Priority.HIGH)
        make(manager, "task-d", priority=Priority.HIGH)
        # task-d ahead of task-c inside their band, so band order and creation order
        # disagree and only one of them can be what comes back.
        manager.move("task-d", top=True, actor="bot")

        assert listed(client) == ["task-b", "task-d", "task-c", "task-a"]

    def test_touching_a_task_does_not_promote_it(self, api) -> None:
        """The exact failure the deleted client sort produced: a note reorders the list.

        Logging progress moves ``updated``, and ``updated`` decided the old order. So
        the answer to "what is at the top of my backlog" changed because somebody wrote
        a note -- with nothing on screen to say why it had moved.
        """
        client, manager, _ = api
        make(manager, "task-a")
        make(manager, "task-b")
        before = listed(client)

        manager.add_log_entry(
            "task-b", actor="bot", type=LogEntryType.PROGRESS, body="Still working on it."
        )

        assert listed(client) == before

    def test_closed_work_sorts_behind_the_whole_queue(self, api) -> None:
        """A closed task keeps its band but has no place in line, so it is not in one.

        Ordering the two together by band alone would file a closed ``critical`` above
        the live ``high`` queue, which reads as urgent work and is finished work.
        """
        client, manager, _ = api
        make(manager, "task-done", priority=Priority.CRITICAL)
        make(manager, "task-open", priority=Priority.LOW)
        manager.close_task("task-done", actor="bot", outcome=Outcome.COMPLETED)

        assert listed(client) == ["task-open", "task-done"]

    # Two cases stood here. Each made a queue "broken" by hand -- stripping
    # `queue_position` off open work, or duplicating one -- straight into a task file,
    # and asserted the listing reported the damage rather than guessing a place or
    # refusing to answer.
    #
    # The state is unrepresentable (task-402). An open task's position is `NOT NULL`
    # with a `ge=1` check, `ux_task_queue_slot` is unique, and there is no file for a
    # bad merge or an editor to reach. The listing's own obligation is unchanged and is
    # covered above: it renders what the store holds, and reports separately whatever
    # the store could not accept.


# ---------------------------------------------------------------------------
# sc-4 -- the dashboard carries the breakage rather than raising over it
# ---------------------------------------------------------------------------


class TestTheDashboardSaysWhatIsNext:
    """What the dashboard's ladder answers when nothing is wrong with the order.

    Three cases stood above this one, each breaking the queue by hand and asserting the
    dashboard reported the damage rather than answering 500 or, worse, "nothing
    claimable" -- the one state in which a human does nothing and feels correct doing
    it. None of them can be set up any more (task-402): a duplicate slot is refused by
    a unique index and there is no file to edit. The panel and its ladder are unchanged
    and the code that fills them is still there; what has gone is the way to produce
    the input.
    """

    def test_a_healthy_queue_carries_no_breakage_and_still_names_what_is_next(self, api) -> None:
        client, manager, _ = api
        make(manager, "task-a")

        body = client.get("/api/dashboard").json()

        assert body["queue_broken"] is None
        assert body["next_action"] == "next_up"
        assert body["next_task"]["id"] == "task-a"
