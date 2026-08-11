"""Sub-task behaviour: children, umbrella non-claimability, and what a parent may be.

The `parent` field has existed since schema v2 (task-050) without anything reading it.
These tests cover what it now *does*: children can be listed, a task with open children
is not claimable, and a parent that does not exist or closes a loop is refused.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentjobs.manager import TaskManager, TaskNotFoundError
from agentjobs.models_v2 import Lifecycle, Outcome, Priority
from agentjobs.storage import TaskStorage

UMBRELLA = "task-100-umbrella"
CHILD_A = "task-101-alpha"
CHILD_B = "task-102-beta"
UNRELATED = "task-200-unrelated"


def _manager(tmp_path: Path) -> TaskManager:
    return TaskManager(TaskStorage(tmp_path))


def _ready(manager: TaskManager, task_id: str, title: str, **kwargs: object) -> None:
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
        assert manager.get_task(UMBRELLA).lifecycle is Lifecycle.READY

    def test_it_becomes_claimable_once_every_child_is_closed(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        _hierarchy(manager)
        for child in (CHILD_A, CHILD_B):
            manager.claim_task(child, agent="codex")
            manager.close_task(child, actor="codex", outcome=Outcome.COMPLETED)

        assert manager.get_next_task().id == UMBRELLA
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
        assert manager.get_task(UMBRELLA).parent is None

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
