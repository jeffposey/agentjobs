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
from agentjobs.models_v2 import Ball, BallReason, Lifecycle, LogEntryType, Outcome, Priority
from agentjobs.projects import Project
from agentjobs.queue import REPAIR_COMMAND
from agentjobs.storage import TaskStorage

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
    yield tmp_path, TaskManager(TaskStorage(tmp_path / "tasks"))


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

    def test_an_open_task_with_no_position_is_a_broken_file_not_a_guess(self, api) -> None:
        """Rule 6 keeps the listing from ever having to invent a place.

        Stripping ``queue_position`` off open work does not produce a task the list has
        to sort somehow; it produces a file that will not load, reported under
        ``/tasks/broken`` where the React list already renders unreadable files. That
        matters here because the alternative -- reading a missing position as ``0`` --
        would put the one task the corpus knows least about at the head of the queue.
        """
        client, manager, root = api
        make(manager, "task-a")
        make(manager, "task-b")
        path = root / "tasks" / "task-b.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        raw.pop("queue_position")
        path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

        assert listed(client) == ["task-a"]
        broken = client.get("/api/tasks/broken").json()
        assert [entry["task_id"] for entry in broken] == ["task-b"]
        assert "queue_position is required" in broken[0]["reason"]

    def test_the_listing_renders_a_broken_queue_instead_of_refusing(self, api) -> None:
        """It is one of the two surfaces that must keep working *because* it is broken."""
        client, manager, root = api
        first = make(manager, "task-a")
        second = make(manager, "task-b")
        break_the_queue(root, second, band="high", at=position(manager, first))

        assert sorted(listed(client)) == ["task-a", "task-b"]


# ---------------------------------------------------------------------------
# sc-4 -- the dashboard carries the breakage rather than raising over it
# ---------------------------------------------------------------------------


class TestDashboardSurvivesABrokenQueue:
    """A duplicated position used to answer 500 here. Now it answers, and says why."""

    def test_it_answers_200_and_names_the_offenders_and_the_repair(self, api) -> None:
        client, manager, root = api
        first = make(manager, "task-a")
        second = make(manager, "task-b")
        break_the_queue(root, second, band="high", at=position(manager, first))

        response = client.get("/api/dashboard")

        assert response.status_code == 200
        broken = response.json()["queue_broken"]
        assert broken is not None
        assert broken["repair_command"] == REPAIR_COMMAND
        named = {task for problem in broken["problems"] for task in problem["tasks"]}
        assert named == {first, second}
        assert any("position" in problem["message"] for problem in broken["problems"])

    def test_it_says_the_queue_is_broken_rather_than_nothing_claimable(self, api) -> None:
        """A corrupt corpus reporting "nothing claimable" reads as an empty backlog.

        Which is the worst available lie: it is the one state in which a human does
        nothing and feels correct doing it.
        """
        client, manager, root = api
        first = make(manager, "task-a")
        second = make(manager, "task-b")
        break_the_queue(root, second, band="high", at=position(manager, first))

        body = client.get("/api/dashboard").json()

        assert body["next_action"] == "queue_broken"
        assert body["next_task"] is None

    def test_work_blocked_on_a_human_still_outranks_the_broken_queue(self, api) -> None:
        """Corruption falsifies "this is next". It does not falsify "you are blocking"."""
        client, manager, root = api
        first = make(manager, "task-a")
        second = make(manager, "task-b")
        make(manager, "task-c")
        manager.claim_task("task-c", agent="bot")
        manager.handoff(
            "task-c",
            actor="bot",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Look at it.",
        )
        break_the_queue(root, second, band="high", at=position(manager, first))

        body = client.get("/api/dashboard").json()

        assert body["next_action"] == "blocked"
        # The banner is not the ladder: it renders whatever the panel says.
        assert body["queue_broken"] is not None

    def test_a_healthy_queue_carries_no_breakage_and_still_names_what_is_next(self, api) -> None:
        client, manager, _ = api
        make(manager, "task-a")

        body = client.get("/api/dashboard").json()

        assert body["queue_broken"] is None
        assert body["next_action"] == "next_up"
        assert body["next_task"]["id"] == "task-a"
