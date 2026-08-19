"""Sub-task behaviour: children, umbrella non-claimability, ?parent=, and the page.

The `parent` field has existed since schema v2 (task-050) without anything reading it.
These tests cover what it now *does*: children can be listed, a task with open children
is not claimable, the API can filter by parent and refuses a parent that does not exist
or that closes a loop, and the detail page shows the hierarchy in both directions.

Rendering assertions check the values a browser acts on, not the presence of markup --
`data-child-status="Ready"`, not `data-child-status=`. A template that emitted
`Lifecycle.READY` would satisfy the second and mean nothing (ENGINEERING.md,
Verification).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterator, List, Tuple

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import TASKS_DIR_ENV, get_task_manager, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.manager import TaskManager, TaskNotFoundError
from agentjobs.models_v2 import Ball, BallReason, Lifecycle, Outcome, Priority
from agentjobs.projects import ProjectRegistry
from agentjobs.storage import TaskStorage

UMBRELLA = "task-100-umbrella"
CHILD_A = "task-101-alpha"
CHILD_B = "task-102-beta"
UNRELATED = "task-200-unrelated"


def _manager(tmp_path: Path) -> TaskManager:
    return TaskManager(TaskStorage(tmp_path))


def _ready(manager: TaskManager, task_id: str, title: str, **kwargs: Any) -> None:
    """Create a ready task -- the only state from which claimability is interesting."""
    manager.create_task(
        id=task_id,
        title=title,
        description=f"Spec for {title}",
        category="ops",
        lifecycle=Lifecycle.READY,
        **kwargs,
    )


def _hierarchy(manager: TaskManager) -> None:
    """One umbrella, two open children, and a task that is nobody's child."""
    _ready(manager, UMBRELLA, "Umbrella", priority=Priority.CRITICAL)
    _ready(manager, CHILD_A, "First child", parent=UMBRELLA)
    _ready(manager, CHILD_B, "Second child", parent=UMBRELLA)
    _ready(manager, UNRELATED, "Unrelated")


# ----------------------------------------------------------------------------------
# Manager: reading the hierarchy
# ----------------------------------------------------------------------------------


class TestGetSubtasks:
    def test_returns_exactly_the_children_in_id_order(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        _hierarchy(manager)

        assert [task.id for task in manager.get_subtasks(UMBRELLA)] == [CHILD_A, CHILD_B]

    def test_a_childless_task_has_no_children(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        _hierarchy(manager)

        assert manager.get_subtasks(UNRELATED) == []

    def test_closed_children_are_still_children(self, tmp_path: Path) -> None:
        """Non-claimability keys on *open* children; the listing keys on all of them."""
        manager = _manager(tmp_path)
        _hierarchy(manager)
        manager.claim_task(CHILD_A, agent="codex")
        manager.close_task(CHILD_A, actor="codex", outcome=Outcome.COMPLETED)

        assert [task.id for task in manager.get_subtasks(UMBRELLA)] == [CHILD_A, CHILD_B]

    def test_an_unknown_id_raises_rather_than_returning_nothing(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        _hierarchy(manager)

        with pytest.raises(TaskNotFoundError, match="task-999"):
            manager.get_subtasks("task-999")


# ----------------------------------------------------------------------------------
# Manager: an umbrella is not work
# ----------------------------------------------------------------------------------


class TestUmbrellaIsNotClaimable:
    def test_next_task_skips_an_umbrella_with_open_children(self, tmp_path: Path) -> None:
        """The umbrella is critical priority, so it would be first if it were eligible."""
        manager = _manager(tmp_path)
        _hierarchy(manager)

        offered = manager.get_next_task()

        assert offered is not None
        assert offered.id != UMBRELLA
        assert offered.id in {CHILD_A, CHILD_B, UNRELATED}

    def test_claim_is_refused_and_names_the_open_children(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        _hierarchy(manager)

        with pytest.raises(ValueError, match="umbrella") as excinfo:
            manager.claim_task(UMBRELLA, agent="claude")

        message = str(excinfo.value)
        assert CHILD_A in message and CHILD_B in message
        umbrella = manager.get_task(UMBRELLA)
        assert umbrella is not None and umbrella.lifecycle is Lifecycle.READY

    def test_it_becomes_claimable_once_every_child_is_closed(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        _hierarchy(manager)
        for child in (CHILD_A, CHILD_B):
            manager.claim_task(child, agent="codex")
            manager.close_task(child, actor="codex", outcome=Outcome.COMPLETED)

        next_task = manager.get_next_task()
        assert next_task is not None and next_task.id == UMBRELLA
        assert manager.claim_task(UMBRELLA, agent="claude").lifecycle is Lifecycle.ACTIVE

    def test_one_open_child_is_enough_to_block_it(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        _hierarchy(manager)
        manager.claim_task(CHILD_A, agent="codex")
        manager.close_task(CHILD_A, actor="codex", outcome=Outcome.COMPLETED)

        with pytest.raises(ValueError, match=CHILD_B):
            manager.claim_task(UMBRELLA, agent="claude")

    def test_a_child_of_its_own_is_unaffected(self, tmp_path: Path) -> None:
        """Only the parent is blocked. A child with no children of its own is work."""
        manager = _manager(tmp_path)
        _hierarchy(manager)

        assert manager.claim_task(CHILD_A, agent="claude").assignment.owner == "claude"


# ----------------------------------------------------------------------------------
# Manager: what a parent may be
# ----------------------------------------------------------------------------------


class TestParentValidation:
    def test_a_parent_that_does_not_exist_is_refused(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        _hierarchy(manager)

        with pytest.raises(ValueError, match="task-does-not-exist' does not exist"):
            _ready(manager, "task-300-orphan", "Orphan", parent="task-does-not-exist")

        assert manager.get_task("task-300-orphan") is None

    def test_a_task_cannot_be_its_own_parent(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        _hierarchy(manager)

        with pytest.raises(ValueError, match="cannot be its own parent"):
            _ready(manager, "task-301-self", "Self", parent="task-301-self")

    def test_a_cycle_is_refused_and_names_the_chain(self, tmp_path: Path) -> None:
        """Parenting the umbrella under its own grandchild would close a loop."""
        manager = _manager(tmp_path)
        _hierarchy(manager)
        _ready(manager, "task-103-grandchild", "Grandchild", parent=CHILD_A)

        with pytest.raises(ValueError, match="cycle") as excinfo:
            manager.update_task(UMBRELLA, parent="task-103-grandchild")

        chain = f"{UMBRELLA} -> {CHILD_A} -> task-103-grandchild"
        assert chain in str(excinfo.value)
        umbrella = manager.get_task(UMBRELLA)
        assert umbrella is not None and umbrella.parent is None

    def test_a_valid_reparent_is_applied(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        _hierarchy(manager)

        assert manager.update_task(UNRELATED, parent=UMBRELLA).parent == UMBRELLA
        assert [task.id for task in manager.get_subtasks(UMBRELLA)] == [
            CHILD_A,
            CHILD_B,
            UNRELATED,
        ]

    def test_a_child_can_be_unparented(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        _hierarchy(manager)

        assert manager.update_task(CHILD_A, parent=None).parent is None
        assert [task.id for task in manager.get_subtasks(UMBRELLA)] == [CHILD_B]


# ----------------------------------------------------------------------------------
# API
# ----------------------------------------------------------------------------------


@pytest.fixture()
def api_client(tmp_path: Path) -> Iterator[Tuple[TestClient, TaskManager]]:
    """A client bound to a temp task directory holding the standard hierarchy."""
    reset_dependency_cache()
    manager = _manager(tmp_path)
    _hierarchy(manager)

    app.dependency_overrides[get_task_manager] = lambda: manager
    with TestClient(app) as client:
        yield client, manager
    app.dependency_overrides.clear()
    reset_dependency_cache()


class TestParentOverTheApi:
    def test_the_filter_returns_exactly_the_children(self, api_client) -> None:
        client, _ = api_client

        response = client.get("/api/tasks", params={"parent": UMBRELLA})

        assert response.status_code == 200
        assert [task["id"] for task in response.json()] == [CHILD_A, CHILD_B]

    def test_an_unfiltered_listing_still_returns_everything(self, api_client) -> None:
        client, _ = api_client

        assert len(client.get("/api/tasks").json()) == 4

    def test_a_parent_with_no_children_filters_to_nothing(self, api_client) -> None:
        client, _ = api_client

        assert client.get("/api/tasks", params={"parent": UNRELATED}).json() == []

    def test_a_task_can_be_created_with_a_parent(self, api_client) -> None:
        client, manager = api_client

        response = client.post(
            "/api/tasks",
            json={
                "title": "Third child",
                "description": "Created under the umbrella",
                "category": "ops",
                "parent": UMBRELLA,
            },
        )

        assert response.status_code == 201
        assert response.json()["parent"] == UMBRELLA
        assert manager.get_task(response.json()["id"]).parent == UMBRELLA

    def test_a_missing_parent_is_a_400(self, api_client) -> None:
        client, _ = api_client

        response = client.post(
            "/api/tasks",
            json={
                "title": "Orphan",
                "description": "Points at nothing",
                "category": "ops",
                "parent": "task-does-not-exist",
            },
        )

        assert response.status_code == 400
        assert "does not exist" in response.json()["detail"]

    def test_self_parenting_is_a_400(self, api_client) -> None:
        client, _ = api_client

        response = client.post(
            "/api/tasks",
            json={
                "id": "task-302-self",
                "title": "Self",
                "description": "Points at itself",
                "category": "ops",
                "parent": "task-302-self",
            },
        )

        assert response.status_code == 400
        assert "own parent" in response.json()["detail"]

    def test_a_cycle_through_patch_is_a_400(self, api_client) -> None:
        client, manager = api_client

        response = client.patch(f"/api/tasks/{UMBRELLA}", json={"parent": CHILD_A})

        assert response.status_code == 400
        assert "cycle" in response.json()["detail"]
        umbrella = manager.get_task(UMBRELLA)
        assert umbrella is not None and umbrella.parent is None

    def test_a_bad_parent_on_a_task_that_does_not_exist_is_still_a_404(self, api_client) -> None:
        """The addressed task is what decides 404; the payload only ever decides 400."""
        client, _ = api_client

        response = client.patch("/api/tasks/task-999", json={"parent": "task-also-missing"})

        assert response.status_code == 404

    def test_next_skips_the_umbrella(self, api_client) -> None:
        client, _ = api_client

        assert client.get("/api/tasks/next").json()["id"] != UMBRELLA

    def test_claiming_the_umbrella_is_refused(self, api_client) -> None:
        client, _ = api_client

        response = client.post(f"/api/tasks/{UMBRELLA}/claim", json={"agent": "claude"})

        assert response.status_code == 409
        assert CHILD_A in response.json()["detail"]


# ----------------------------------------------------------------------------------
# The page
# ----------------------------------------------------------------------------------


@pytest.fixture()
def web_client(tmp_path: Path, monkeypatch) -> Iterator[Tuple[TestClient, TaskManager]]:
    """A registered project holding the hierarchy, served through the real wiring."""
    monkeypatch.setenv("AGENTJOBS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(TASKS_DIR_ENV, raising=False)
    monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    reset_dependency_cache()

    root = tmp_path / "solo"
    (root / ".agentjobs").mkdir(parents=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump({"project_name": "Solo", "tasks_directory": "tasks"}),
        encoding="utf-8",
    )
    manager = TaskManager(TaskStorage(root / "tasks"))
    _hierarchy(manager)
    ProjectRegistry(home=tmp_path / "home").add(root, project_id="solo")

    with TestClient(app) as client:
        yield client, manager

    reset_dependency_cache()


class TestHierarchyOnTheDetailPage:
    def test_the_umbrella_lists_its_children(self, web_client) -> None:
        client, _ = web_client

        page = client.get(f"/p/solo/tasks/{UMBRELLA}").text

        assert "Sub-tasks" in page
        assert CHILD_A in page and CHILD_B in page
        assert "First child" in page and "Second child" in page

    def test_each_child_links_to_its_own_page_within_the_project(self, web_client) -> None:
        client, _ = web_client

        page = client.get(f"/p/solo/tasks/{UMBRELLA}").text

        assert f'href="/p/solo/tasks/{CHILD_A}"' in page

    def test_children_carry_their_own_rendered_status(self, web_client) -> None:
        """The badge shows each child's state, not the parent's, and not an enum repr."""
        client, manager = web_client
        manager.claim_task(CHILD_A, agent="codex")
        manager.handoff(
            CHILD_A,
            actor="codex",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Look at the diff.",
        )

        page = client.get(f"/p/solo/tasks/{UMBRELLA}").text

        assert 'data-child-status="Needs review"' in page
        assert 'data-child-status="Ready"' in page
        assert "Lifecycle." not in page and "Ball." not in page

    def test_the_umbrella_says_why_it_is_not_claimable(self, web_client) -> None:
        client, _ = web_client

        page = client.get(f"/p/solo/tasks/{UMBRELLA}").text

        assert "2 open" in page

    def test_the_rollup_counts_only_completed_children(self, web_client) -> None:
        client, manager = web_client
        manager.claim_task(CHILD_A, agent="codex")
        manager.close_task(CHILD_A, actor="codex", outcome=Outcome.COMPLETED)

        page = client.get(f"/p/solo/tasks/{UMBRELLA}").text

        assert "1 of 2 complete" in page
        assert 'style="width: 50%"' in page

    def test_an_abandoned_child_is_finished_but_not_complete(self, web_client) -> None:
        """Counting it as complete would let an effort report itself done because half
        of it was cancelled."""
        client, manager = web_client
        manager.claim_task(CHILD_A, agent="codex")
        manager.close_task(CHILD_A, actor="codex", outcome=Outcome.CANCELLED)

        page = client.get(f"/p/solo/tasks/{UMBRELLA}").text

        assert "0 of 2 complete" in page
        assert 'style="width: 0%"' in page

    def test_the_rollup_names_the_child_waiting_on_a_human(self, web_client) -> None:
        client, manager = web_client
        manager.claim_task(CHILD_A, agent="codex")
        manager.handoff(
            CHILD_A,
            actor="codex",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Look at the diff.",
        )

        page = client.get(f"/p/solo/tasks/{UMBRELLA}").text

        assert "Waiting on a human:" in page
        assert f'href="/p/solo/tasks/{CHILD_A}" class="font-mono hover:underline"' in page

    def test_the_rollup_names_a_child_in_progress(self, web_client) -> None:
        client, manager = web_client
        manager.claim_task(CHILD_B, agent="codex")

        page = client.get(f"/p/solo/tasks/{UMBRELLA}").text

        assert "In progress:" in page
        assert CHILD_B in page

    def test_a_flat_task_has_no_rollup(self, web_client) -> None:
        client, _ = web_client

        page = client.get(f"/p/solo/tasks/{UNRELATED}").text

        assert "complete ·" not in page

    def test_a_child_links_back_to_its_parent(self, web_client) -> None:
        client, _ = web_client

        page = client.get(f"/p/solo/tasks/{CHILD_A}").text

        assert f'href="/p/solo/tasks/{UMBRELLA}"' in page
        assert "Part of" in page
        assert "Umbrella" in page

    def test_a_flat_task_shows_neither_section(self, web_client) -> None:
        client, _ = web_client

        page = client.get(f"/p/solo/tasks/{UNRELATED}").text

        assert "Sub-tasks" not in page
        assert "Part of" not in page


def _row_ids(page: str) -> List[str]:
    """The task ids of the list's rows, in the order the table draws them."""
    return re.findall(r'<tr\b[^>]*\bdata-id="([^"]+)"', page)


def _ancestors(page: str, task_id: str) -> str:
    """The data-ancestors value of one row -- what governs whether it is drawn."""
    match = re.search(
        rf'<tr\b[^>]*\bdata-id="{re.escape(task_id)}"[^>]*\bdata-ancestors="([^"]*)"',
        page,
    )
    assert match is not None, f"no row for {task_id}"
    return match.group(1)


class TestHierarchyInTheTaskList:
    def test_children_are_drawn_under_their_parent_in_id_order(self, web_client) -> None:
        """Id order among siblings, matching get_subtasks and the detail page.

        The table itself sorts by recency, which for numbered stages under one umbrella
        would draw 054 above 050 -- the same five tasks in a different order on two
        pages of the same UI.
        """
        client, _ = web_client

        ids = _row_ids(client.get("/p/solo/tasks").text)
        start = ids.index(UMBRELLA)

        assert ids[start + 1 : start + 3] == [CHILD_A, CHILD_B]

    def test_every_task_is_drawn_exactly_once(self, web_client) -> None:
        client, _ = web_client

        ids = _row_ids(client.get("/p/solo/tasks").text)

        assert sorted(ids) == sorted([UMBRELLA, CHILD_A, CHILD_B, UNRELATED])

    def test_a_child_row_names_the_ancestors_that_govern_it(self, web_client) -> None:
        """The value, not the attribute: the JS hides a row whose ancestor is collapsed."""
        client, _ = web_client

        page = client.get("/p/solo/tasks").text

        assert _ancestors(page, CHILD_A) == UMBRELLA
        assert _ancestors(page, UMBRELLA) == ""
        assert _ancestors(page, UNRELATED) == ""

    def test_a_grandchild_names_the_whole_chain(self, web_client) -> None:
        client, manager = web_client
        _ready(manager, "task-103-grandchild", "Grandchild", parent=CHILD_A)

        page = client.get("/p/solo/tasks").text

        assert _ancestors(page, "task-103-grandchild") == f"{UMBRELLA},{CHILD_A}"

    def test_a_parent_row_states_how_many_children_it_has(self, web_client) -> None:
        client, _ = web_client

        page = client.get("/p/solo/tasks").text

        assert "2 sub-tasks, 2 open" in page

    def test_the_count_is_singular_for_one_child(self, web_client) -> None:
        client, manager = web_client
        _ready(manager, "task-104-only", "Only child", parent=UNRELATED)

        page = client.get("/p/solo/tasks").text

        assert "1 sub-task, 1 open" in page

    def test_a_closed_child_counts_as_a_child_but_not_as_open(self, web_client) -> None:
        client, manager = web_client
        manager.claim_task(CHILD_A, agent="codex")
        manager.close_task(CHILD_A, actor="codex", outcome=Outcome.COMPLETED)

        page = client.get("/p/solo/tasks").text

        assert "2 sub-tasks, 1 open" in page

    def test_a_task_pointing_at_a_parent_that_does_not_exist_is_still_drawn(
        self, web_client, tmp_path: Path
    ) -> None:
        """The manager refuses to write one; a hand-edited file can still carry one.

        Treating it as a root is a judgement call. Dropping the row is not available:
        a task that vanishes from the listing is invisible exactly when someone needs
        to notice it is wrong.
        """
        client, manager = web_client
        stray = manager.get_task(UNRELATED).model_copy(
            update={"id": "task-900-stray", "parent": "task-nope"}
        )
        manager.storage.save_task(stray)

        page = client.get("/p/solo/tasks").text

        assert "task-900-stray" in _row_ids(page)
        assert _ancestors(page, "task-900-stray") == ""
