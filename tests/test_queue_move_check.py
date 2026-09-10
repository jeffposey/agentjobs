"""The queue-move check: deterministic warnings when a human reorders (task-219).

Section 5.4 of ``docs/playbooks-design.md`` and decision P8 (revised). The move lands
and is authoritative; the response carries what the move is worth saying.

**The test that matters most is** :class:`TestSilenceIsNormal`. Everything else here
proves a warning fires on its own construction, and that one proves it does not fire
on anything else -- which is the whole difference between a check somebody reads and
wallpaper somebody clicks past.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agentjobs.api.dependencies import get_task_manager, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.api.routes.status import get_acting_project
from agentjobs.cli import app as cli_app
from agentjobs.instrumentation import count_task_parses
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Lifecycle, LogEntryType, Priority
from agentjobs.queue import Placement, band_entries
from agentjobs.queue_check import (
    ABOVE_PREREQUISITE,
    ANCHOR_KEY,
    DEMOTED_BLOCKER,
    KEPT_OVER_KEY,
    NO_OP,
    PROMOTED_UNCLAIMABLE,
    STRONG_ANCHOR,
    MoveCheck,
    apply_placement,
    check_move,
    undo_placement,
)
from agentjobs.projects import Project
from support import task_store

runner = CliRunner()

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
    """A project directory with config and an empty tasks directory."""
    (tmp_path / ".agentjobs").mkdir(parents=True)
    (tmp_path / ".agentjobs" / "config.yaml").write_text(yaml.safe_dump(CONFIG), encoding="utf-8")
    yield tmp_path, TaskManager(task_store(tmp_path / "tasks"))


@pytest.fixture()
def api(project) -> Iterator[Tuple[TestClient, TaskManager]]:
    """A TestClient bound to the fixture project, acting as that project."""
    root, manager = project
    reset_dependency_cache()
    acting = Project(id="fixture", name="Fixture", root=root)
    app.dependency_overrides[get_task_manager] = lambda: manager
    app.dependency_overrides[get_acting_project] = lambda: acting
    with TestClient(app) as client:
        yield client, manager
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


def needs(task_id: str) -> Dict[str, Any]:
    """A `needs` dependency, in the shape create_task takes one."""
    return {"dependencies": [{"task": task_id, "type": "needs"}]}


def order(manager: TaskManager, priority: Priority = Priority.HIGH) -> List[str]:
    """The band as it stands on disk, in queue order."""
    return [
        entry.task_id for entry in band_entries(manager.storage.list_tasks_uncached(), priority)
    ]


def kinds(outcome: Any) -> List[str]:
    return [warning.kind for warning in outcome.warnings]


def seed(manager: TaskManager, count: int = 5) -> List[str]:
    """A plain band of unblocked, unrelated, claimable tasks."""
    return [make(manager, f"task-{index:03d}-work") for index in range(1, count + 1)]


# ---------------------------------------------------------------------------
# sc-2 -- the one the design's "silence is the normal outcome" rests on
# ---------------------------------------------------------------------------


class TestSilenceIsNormal:
    """A legal move says nothing. A check that speaks on ordinary moves is wallpaper."""

    def test_every_ordinary_move_in_a_plain_band_is_silent(self, project) -> None:
        _, manager = project
        ids = seed(manager, 6)

        moves = [
            dict(task_id=ids[3], top=True),
            dict(task_id=ids[0], bottom=True),
            dict(task_id=ids[5], before=ids[1]),
            dict(task_id=ids[2], after=ids[4]),
            dict(task_id=ids[1], before=ids[0]),
        ]
        for move in moves:
            outcome = manager.move_with_warnings(actor="Ada", **move)
            assert outcome.warnings == (), f"{move} should have been silent: {outcome.warnings}"
            assert outcome.undo is None

    def test_a_move_that_is_silent_still_records_no_warnings_key(self, project) -> None:
        """A clean move leaves the entry exactly as it was before the check existed."""
        _, manager = project
        ids = seed(manager, 3)
        manager.move_with_warnings(ids[2], top=True, actor="Ada")
        entry = [
            item for item in manager.get_task(ids[2]).log if item.type is LogEntryType.QUEUE_MOVE
        ][-1]
        assert "warnings" not in entry.data
        assert "undo" not in entry.data

    def test_unrelated_blocked_work_elsewhere_in_the_band_is_silent(self, project) -> None:
        """The check is about *this* move, not an audit of the band it happened in."""
        _, manager = project
        make(manager, "task-001-gate")
        make(manager, "task-002-blocked", **needs("task-001-gate"))
        ids = [make(manager, f"task-{index:03d}-work") for index in (3, 4, 5)]

        outcome = manager.move_with_warnings(ids[2], before=ids[0], actor="Ada")
        assert outcome.warnings == ()


# ---------------------------------------------------------------------------
# sc-1 -- each class fires on its own construction, and on no other
# ---------------------------------------------------------------------------


class TestPromotedUnclaimable:
    """The common case: a task promoted to a place the queue will skip past."""

    def test_promoting_a_blocked_task_warns(self, project) -> None:
        """The gate is in another band, so this construction earns exactly one warning."""
        _, manager = project
        make(manager, "task-001-gate", priority=Priority.LOW)
        seed_ids = [make(manager, f"task-{index:03d}-work") for index in (2, 3)]
        make(manager, "task-004-blocked", **needs("task-001-gate"))

        outcome = manager.move_with_warnings("task-004-blocked", top=True, actor="Ada")
        assert kinds(outcome) == [PROMOTED_UNCLAIMABLE]
        message = outcome.warnings[0].message
        # The rendered sentence, not the presence of a warning object: this is the
        # existing `_skip_reason` text, and a reader has to be told which dependency.
        assert "task-004-blocked cannot be claimed" in message
        assert "task-001-gate (still open)" in message
        assert "skip past it" in message
        assert outcome.warnings[0].tasks == ("task-004-blocked",)
        assert seed_ids  # the band had other members; the warning named only the mover

    def test_the_ordinary_same_band_promotion_earns_all_three(self, project) -> None:
        """One drag, three true things -- and the design says all three are worth saying.

        Dragging a blocked task to the top of its own band is the mistake §5.4 is
        written about, and it constructs every consequence warning at once: the task
        will be skipped, it now stands above the thing it waits on, and the thing it
        waits on has been pushed down. They are three different repairs, so they are
        three sentences rather than one.
        """
        _, manager = project
        make(manager, "task-001-gate")
        for index in (2, 3):
            make(manager, f"task-{index:03d}-work")
        make(manager, "task-004-blocked", **needs("task-001-gate"))

        outcome = manager.move_with_warnings("task-004-blocked", top=True, actor="Ada")
        assert kinds(outcome) == [
            PROMOTED_UNCLAIMABLE,
            ABOVE_PREREQUISITE,
            DEMOTED_BLOCKER,
        ]

    def test_promoting_a_task_with_open_children_warns(self, project) -> None:
        _, manager = project
        seed(manager, 2)
        make(manager, "task-003-epic")
        make(manager, "task-004-child", parent="task-003-epic")

        outcome = manager.move_with_warnings("task-003-epic", top=True, actor="Ada")
        assert kinds(outcome) == [PROMOTED_UNCLAIMABLE]
        assert "has 1 open child" in outcome.warnings[0].message

    def test_promoting_a_draft_warns(self, project) -> None:
        _, manager = project
        seed(manager, 2)
        make(manager, "task-003-draft", lifecycle=Lifecycle.DRAFT)

        outcome = manager.move_with_warnings("task-003-draft", top=True, actor="Ada")
        assert kinds(outcome) == [PROMOTED_UNCLAIMABLE]
        assert "not ready (draft" in outcome.warnings[0].message

    def test_demoting_a_blocked_task_says_nothing(self, project) -> None:
        """Moving blocked work *down* is the fix, not the mistake."""
        _, manager = project
        make(manager, "task-001-gate")
        make(manager, "task-002-blocked", **needs("task-001-gate"))
        make(manager, "task-003-work")

        outcome = manager.move_with_warnings("task-002-blocked", bottom=True, actor="Ada")
        assert outcome.warnings == ()


class TestAbovePrerequisite:
    """The order just written cannot execute in the order it reads."""

    def test_moving_a_task_above_something_it_needs_warns(self, project) -> None:
        _, manager = project
        make(manager, "task-001-first")
        make(manager, "task-002-gate")
        make(manager, "task-003-dependent", **needs("task-002-gate"))

        outcome = manager.move_with_warnings("task-003-dependent", top=True, actor="Ada")
        found = {warning.kind: warning for warning in outcome.warnings}
        assert ABOVE_PREREQUISITE in found
        assert "ahead of task-002-gate, which it needs" in found[ABOVE_PREREQUISITE].message
        assert found[ABOVE_PREREQUISITE].tasks == ("task-002-gate",)

    def test_demoting_a_prerequisite_below_its_dependent_warns(self, project) -> None:
        """The same inversion, constructed from the other end."""
        _, manager = project
        make(manager, "task-001-gate")
        make(manager, "task-002-dependent", **needs("task-001-gate"))
        make(manager, "task-003-work")

        outcome = manager.move_with_warnings("task-001-gate", bottom=True, actor="Ada")
        found = {warning.kind: warning for warning in outcome.warnings}
        assert ABOVE_PREREQUISITE in found
        assert "behind task-002-dependent, which needs it" in found[ABOVE_PREREQUISITE].message

    def test_a_prerequisite_in_another_band_is_not_an_inversion(self, project) -> None:
        """Positions are never compared across bands, so neither is this."""
        _, manager = project
        make(manager, "task-001-gate", priority=Priority.LOW)
        make(manager, "task-002-work")
        make(manager, "task-003-dependent", **needs("task-001-gate"))

        outcome = manager.move_with_warnings("task-003-dependent", top=True, actor="Ada")
        # It is still unclaimable, which is a different warning and the honest one.
        assert kinds(outcome) == [PROMOTED_UNCLAIMABLE]

    def test_a_closed_prerequisite_is_not_an_inversion(self, project) -> None:
        _, manager = project
        make(manager, "task-001-gate")
        make(manager, "task-002-work")
        make(manager, "task-003-dependent", **needs("task-001-gate"))
        manager.close_task("task-001-gate", actor="bot", outcome="completed")

        outcome = manager.move_with_warnings("task-003-dependent", top=True, actor="Ada")
        assert outcome.warnings == ()


class TestDemotedBlocker:
    """What got pushed down is usually the risk in a drag, not what was grabbed."""

    def test_promoting_over_a_gate_names_what_it_gates(self, project) -> None:
        _, manager = project
        make(manager, "task-001-gate")
        for index in (2, 3, 4):
            make(manager, f"task-{index:03d}-waiting", **needs("task-001-gate"))
        make(manager, "task-005-shiny")

        outcome = manager.move_with_warnings("task-005-shiny", top=True, actor="Ada")
        found = {warning.kind: warning for warning in outcome.warnings}
        assert DEMOTED_BLOCKER in found
        assert "task-001-gate (3 open tasks need it)" in found[DEMOTED_BLOCKER].message
        assert found[DEMOTED_BLOCKER].tasks == ("task-001-gate",)

    def test_one_dependent_reads_as_one(self, project) -> None:
        _, manager = project
        make(manager, "task-001-gate")
        make(manager, "task-002-waiting", **needs("task-001-gate"))
        make(manager, "task-003-shiny")

        outcome = manager.move_with_warnings("task-003-shiny", top=True, actor="Ada")
        found = {warning.kind: warning for warning in outcome.warnings}
        assert "task-001-gate (1 open task needs it)" in found[DEMOTED_BLOCKER].message

    def test_demoting_a_gate_itself_warns(self, project) -> None:
        _, manager = project
        make(manager, "task-001-gate")
        make(manager, "task-002-waiting", **needs("task-001-gate"))
        make(manager, "task-003-work")

        outcome = manager.move_with_warnings("task-001-gate", bottom=True, actor="Ada")
        assert DEMOTED_BLOCKER in kinds(outcome)

    def test_demoting_a_task_nothing_waits_on_is_silent(self, project) -> None:
        _, manager = project
        ids = seed(manager, 4)
        outcome = manager.move_with_warnings(ids[3], top=True, actor="Ada")
        assert outcome.warnings == ()

    def test_a_closed_dependent_does_not_count(self, project) -> None:
        """`unblocks_count` is about open work; a finished dependent gates nothing."""
        _, manager = project
        make(manager, "task-001-gate")
        make(manager, "task-002-waiting", **needs("task-001-gate"))
        make(manager, "task-003-shiny")
        manager.close_task("task-002-waiting", actor="bot", outcome="completed")

        outcome = manager.move_with_warnings("task-003-shiny", top=True, actor="Ada")
        assert outcome.warnings == ()


class TestNoOp:
    """A move that changed no order changed nothing about the queue."""

    def test_moving_the_head_to_the_top_warns(self, project) -> None:
        _, manager = project
        ids = seed(manager, 3)
        outcome = manager.move_with_warnings(ids[0], top=True, actor="Ada")
        assert kinds(outcome) == [NO_OP]
        assert "Nothing moved" in outcome.warnings[0].message
        assert ids[0] in outcome.warnings[0].message

    def test_moving_the_tail_to_the_bottom_warns(self, project) -> None:
        _, manager = project
        ids = seed(manager, 3)
        outcome = manager.move_with_warnings(ids[-1], bottom=True, actor="Ada")
        assert kinds(outcome) == [NO_OP]

    def test_placing_a_task_after_the_one_it_already_follows_warns(self, project) -> None:
        _, manager = project
        ids = seed(manager, 3)
        outcome = manager.move_with_warnings(ids[1], after=ids[0], actor="Ada")
        assert kinds(outcome) == [NO_OP]

    def test_a_no_op_suppresses_the_consequence_warnings(self, project) -> None:
        """It caused nothing, so nothing it caused is worth a sentence."""
        _, manager = project
        make(manager, "task-001-gate")
        make(manager, "task-002-blocked", **needs("task-001-gate"))
        outcome = manager.move_with_warnings("task-002-blocked", bottom=True, actor="Ada")
        assert kinds(outcome) == [NO_OP]

    def test_a_no_op_offers_no_undo(self, project) -> None:
        """Undoing a move that changed no order is offering to repeat it."""
        _, manager = project
        ids = seed(manager, 3)
        outcome = manager.move_with_warnings(ids[0], top=True, actor="Ada")
        assert outcome.undo is None
        entry = [item for item in outcome.task.log if item.type is LogEntryType.QUEUE_MOVE][-1]
        assert "undo" not in entry.data

    def test_a_real_move_is_not_a_no_op(self, project) -> None:
        _, manager = project
        ids = seed(manager, 3)
        outcome = manager.move_with_warnings(ids[2], top=True, actor="Ada")
        assert NO_OP not in kinds(outcome)


# The classes that made a queue "broken" by hand stood here. Each wrote a duplicate or
# missing `queue_position` straight into a task file -- "as a bad merge or a hand-edit
# produces it" -- and then asserted that this surface reports the damage instead of
# guessing or raising.
#
# The state is unrepresentable (task-402). An open task's position is `NOT NULL` with a
# `ge=1` check, `ux_task_queue_slot` is unique, and there is no file for a merge or an
# editor to reach; an import that carried such a record quarantines it rather than
# writing it. The detection code is kept as defence -- a constraint that stops being
# enforced would otherwise be silent -- but nothing in this suite can produce the input
# for it any more, and a test that faked one would be asserting against a state the
# product refuses to have.


# ---------------------------------------------------------------------------
# The record: what a later reader, and task-217, can see
# ---------------------------------------------------------------------------


class TestTheRecord:
    def test_warnings_are_written_into_the_queue_move_entry(self, project) -> None:
        _, manager = project
        make(manager, "task-001-gate")
        make(manager, "task-002-blocked", **needs("task-001-gate"))
        make(manager, "task-003-work")

        manager.move_with_warnings("task-002-blocked", top=True, actor="Ada")
        entry = [
            item
            for item in manager.get_task("task-002-blocked").log
            if item.type is LogEntryType.QUEUE_MOVE
        ][-1]
        recorded = entry.data["warnings"]
        assert [item["kind"] for item in recorded] == [
            PROMOTED_UNCLAIMABLE,
            ABOVE_PREREQUISITE,
            DEMOTED_BLOCKER,
        ]
        assert entry.data["undo"] == {"kind": "after", "target": "task-001-gate"}

    def test_undo_puts_the_band_back(self, project) -> None:
        _, manager = project
        make(manager, "task-001-gate")
        make(manager, "task-002-blocked", **needs("task-001-gate"))
        make(manager, "task-003-work")
        was = order(manager)

        outcome = manager.move_with_warnings("task-002-blocked", top=True, actor="Ada")
        assert order(manager) != was
        assert outcome.undo is not None

        placement = outcome.undo
        manager.move_with_warnings(
            "task-002-blocked",
            top=placement.kind == Placement.TOP,
            bottom=placement.kind == Placement.BOTTOM,
            before=placement.target if placement.kind == Placement.BEFORE else None,
            after=placement.target if placement.kind == Placement.AFTER else None,
            actor="Ada",
        )
        assert order(manager) == was

    def test_an_undo_is_an_ordinary_logged_move(self, project) -> None:
        _, manager = project
        make(manager, "task-001-gate")
        make(manager, "task-002-blocked", **needs("task-001-gate"))
        outcome = manager.move_with_warnings("task-002-blocked", top=True, actor="Ada")
        assert outcome.undo is not None
        manager.move_with_warnings("task-002-blocked", after="task-001-gate", actor="Ada")

        moves = [
            item
            for item in manager.get_task("task-002-blocked").log
            if item.type is LogEntryType.QUEUE_MOVE
        ]
        assert len(moves) == 2
        assert moves[-1].actor == "Ada"

    def test_a_group_move_offers_no_undo(self, project) -> None:
        """The inverse of a group move is a group move this record cannot express."""
        _, manager = project
        make(manager, "task-001-gate")
        make(manager, "task-002-epic")
        make(manager, "task-003-child", parent="task-002-epic")
        make(manager, "task-004-work", **needs("task-001-gate"))

        outcome = manager.move_with_warnings(
            "task-002-epic", top=True, with_children=True, actor="Ada"
        )
        assert outcome.warnings  # it has open children, so it is unclaimable
        assert outcome.undo is None

    def test_a_replayed_move_answers_with_the_original_warnings(self, project) -> None:
        _, manager = project
        make(manager, "task-001-gate")
        make(manager, "task-002-blocked", **needs("task-001-gate"))

        first = manager.move_with_warnings(
            "task-002-blocked", top=True, actor="Ada", operation_id="op-1"
        )
        again = manager.move_with_warnings(
            "task-002-blocked", top=True, actor="Ada", operation_id="op-1"
        )
        assert kinds(again) == kinds(first)
        assert again.warnings[0].message == first.warnings[0].message
        assert again.undo == first.undo


class TestKeep:
    """`keep` writes the strong anchor task-217 must respect."""

    def _warned_move(self, manager: TaskManager) -> None:
        make(manager, "task-001-gate")
        make(manager, "task-002-blocked", **needs("task-001-gate"))
        make(manager, "task-003-work")
        manager.move_with_warnings("task-002-blocked", top=True, actor="Ada")

    def test_keep_records_the_anchor_threaded_to_the_move(self, project) -> None:
        _, manager = project
        self._warned_move(manager)

        task = manager.keep_queue_move("task-002-blocked", actor="Ada")
        anchors = [item for item in task.log if item.data.get(ANCHOR_KEY) == STRONG_ANCHOR]
        assert len(anchors) == 1
        anchor = anchors[0]
        assert anchor.type is LogEntryType.DECISION
        assert anchor.actor == "Ada"
        assert anchor.data["band"] == Priority.HIGH.value
        assert anchor.data["queue_position"] == task.queue_position
        assert [item["kind"] for item in anchor.data[KEPT_OVER_KEY]] == [
            PROMOTED_UNCLAIMABLE,
            ABOVE_PREREQUISITE,
            DEMOTED_BLOCKER,
        ]

        move = [item for item in task.log if item.type is LogEntryType.QUEUE_MOVE][-1]
        assert anchor.re == move.id

    def test_keep_is_refused_when_the_move_was_clean(self, project) -> None:
        _, manager = project
        ids = seed(manager, 3)
        manager.move_with_warnings(ids[2], top=True, actor="Ada")
        with pytest.raises(ValueError, match="no warnings"):
            manager.keep_queue_move(ids[2], actor="Ada")

    def test_keep_is_refused_on_a_task_that_never_moved(self, project) -> None:
        _, manager = project
        ids = seed(manager, 2)
        with pytest.raises(ValueError, match="never been moved"):
            manager.keep_queue_move(ids[0], actor="Ada")

    def test_keeping_twice_writes_one_anchor(self, project) -> None:
        _, manager = project
        self._warned_move(manager)
        manager.keep_queue_move("task-002-blocked", actor="Ada")
        task = manager.keep_queue_move("task-002-blocked", actor="Ada")
        anchors = [item for item in task.log if item.data.get(ANCHOR_KEY) == STRONG_ANCHOR]
        assert len(anchors) == 1

    def test_a_later_warned_move_can_be_kept_again(self, project) -> None:
        """The anchor is about one move, so a second one is a second decision."""
        _, manager = project
        self._warned_move(manager)
        manager.keep_queue_move("task-002-blocked", actor="Ada")
        manager.move_with_warnings("task-002-blocked", bottom=True, actor="Ada")
        manager.move_with_warnings("task-002-blocked", top=True, actor="Ada")
        task = manager.keep_queue_move("task-002-blocked", actor="Ada")
        anchors = [item for item in task.log if item.data.get(ANCHOR_KEY) == STRONG_ANCHOR]
        assert len(anchors) == 2


# ---------------------------------------------------------------------------
# sc-5 -- the check costs nothing the move was not already paying
# ---------------------------------------------------------------------------


class TestTheCheckIsFree:
    def test_computing_the_warnings_parses_no_files(self, project) -> None:
        """The direct claim: nothing between the corpus read and the warnings is I/O.

        Asserted on task parses rather than wall clock, per the performance convention
        -- the count means the same thing on every machine.
        """
        _, manager = project
        make(manager, "task-001-gate")
        make(manager, "task-002-blocked", **needs("task-001-gate"))
        make(manager, "task-003-work")

        tasks = manager.storage.list_tasks_uncached()
        moved = next(task for task in tasks if task.id == "task-002-blocked")
        before = [entry.task_id for entry in band_entries(tasks, Priority.HIGH)]
        after = apply_placement(before, [moved.id], Placement(Placement.TOP))

        with count_task_parses() as tally:
            warnings = check_move(manager._move_facts(tasks, moved, Priority.HIGH, before, after))
        assert tally.parses == 0
        assert [warning.kind for warning in warnings] == [
            PROMOTED_UNCLAIMABLE,
            ABOVE_PREREQUISITE,
            DEMOTED_BLOCKER,
        ]

    def test_a_warned_move_reads_no_more_than_a_clean_one(self, project) -> None:
        """The whole verb, compared against itself on a corpus with nothing to say.

        Two moves of the same size over corpora of the same size: one that earns three
        warnings and one that earns none. The check is inside both, so an implementation
        that went back to storage for the dependency graph would show up here as the
        warned move costing more.
        """
        root, quiet = project
        seed(quiet, 6)
        with count_task_parses() as clean:
            quiet.move_with_warnings("task-006-work", top=True, actor="Ada")

        loud_root = Path(str(root) + "-loud")
        (loud_root / ".agentjobs").mkdir(parents=True)
        (loud_root / ".agentjobs" / "config.yaml").write_text(
            yaml.safe_dump(CONFIG), encoding="utf-8"
        )
        loud = TaskManager(task_store(loud_root / "tasks"))
        make(loud, "task-001-gate")
        for index in (2, 3, 4):
            make(loud, f"task-{index:03d}-waiting", **needs("task-001-gate"))
        make(loud, "task-005-work")
        make(loud, "task-006-blocked", **needs("task-001-gate"))
        with count_task_parses() as noisy:
            outcome = loud.move_with_warnings("task-006-blocked", top=True, actor="Ada")

        assert len(outcome.warnings) == 3
        assert noisy.parses == clean.parses


# ---------------------------------------------------------------------------
# The pure layer, exercised where the manager cannot reach
# ---------------------------------------------------------------------------


class TestPurePlacement:
    def test_apply_placement_survives_a_renumber(self) -> None:
        """It reads a placement, never a number, which is why a rebalance cannot fool it."""
        band = ["a", "b", "c", "d"]
        assert apply_placement(band, ["c"], Placement(Placement.TOP)) == ["c", "a", "b", "d"]
        assert apply_placement(band, ["a"], Placement(Placement.BOTTOM)) == ["b", "c", "d", "a"]
        assert apply_placement(band, ["d"], Placement(Placement.BEFORE, "b")) == [
            "a",
            "d",
            "b",
            "c",
        ]
        assert apply_placement(band, ["a"], Placement(Placement.AFTER, "c")) == [
            "b",
            "c",
            "a",
            "d",
        ]

    def test_a_group_stays_contiguous_and_keeps_its_order(self) -> None:
        band = ["a", "b", "c", "d"]
        assert apply_placement(band, ["b", "d"], Placement(Placement.TOP)) == [
            "b",
            "d",
            "a",
            "c",
        ]

    def test_a_missing_target_falls_to_the_bottom(self) -> None:
        """Matching plan_insertion rather than inventing a different answer."""
        assert apply_placement(["a", "b"], ["a"], Placement(Placement.AFTER, "zz")) == ["b", "a"]

    def test_undo_placement_names_the_neighbour_above(self) -> None:
        assert undo_placement(["a", "b", "c"], "c") == Placement(Placement.AFTER, "b")
        assert undo_placement(["a", "b", "c"], "a") == Placement(Placement.TOP)
        assert undo_placement(["a", "b"], "zz") is None

    def test_the_named_list_counts_what_it_leaves_out(self) -> None:
        """No silent caps: a message that names three of five says so."""
        warnings = check_move(
            MoveCheck(
                moved="m",
                band="high",
                before=("a", "b", "c", "d", "e", "m"),
                after=("m", "a", "b", "c", "d", "e"),
                unblocks={name: 1 for name in "abcde"},
            )
        )
        assert [warning.kind for warning in warnings] == [DEMOTED_BLOCKER]
        assert "and 2 more" in warnings[0].message
        assert len(warnings[0].tasks) == 5


# ---------------------------------------------------------------------------
# sc-3 -- one implementation, reached from every surface
# ---------------------------------------------------------------------------


def blocked_band(manager: TaskManager) -> None:
    """The construction §5.4 is written about: a blocked task, and work ahead of it."""
    make(manager, "task-001-gate")
    make(manager, "task-002-work")
    make(manager, "task-003-blocked", **needs("task-001-gate"))


class TestTheRestSurface:
    def test_the_envelope_carries_the_warnings_and_the_undo(self, api) -> None:
        client, manager = api
        blocked_band(manager)
        response = client.post(
            "/api/tasks/task-003-blocked/queue-move?envelope=true",
            json={"actor": "Ada", "operation_id": "op-1", "top": True},
        )
        assert response.status_code == 200
        body = response.json()
        assert [item["kind"] for item in body["queue_warnings"]] == [
            PROMOTED_UNCLAIMABLE,
            ABOVE_PREREQUISITE,
            DEMOTED_BLOCKER,
        ]
        # The rendered sentence a browser will show, not the shape of the object.
        assert "cannot be claimed" in body["queue_warnings"][0]["message"]
        assert body["queue_undo"] == {"kind": "after", "target": "task-002-work"}

    def test_a_clean_move_answers_with_an_empty_list(self, api) -> None:
        client, manager = api
        seed(manager, 3)
        response = client.post(
            "/api/tasks/task-003-work/queue-move?envelope=true",
            json={"actor": "Ada", "operation_id": "op-1", "before": "task-001-work"},
        )
        assert response.json()["queue_warnings"] == []
        assert response.json()["queue_undo"] is None

    def test_every_other_verb_still_answers_with_an_empty_list(self, api) -> None:
        """The field is on the shared envelope, so it must be inert everywhere else."""
        client, manager = api
        seed(manager, 2)
        response = client.post(
            "/api/tasks/task-001-work/claim?envelope=true",
            json={"agent": "bot", "operation_id": "op-1"},
        )
        assert response.status_code == 200
        assert response.json()["queue_warnings"] == []

    def test_without_the_envelope_the_response_is_unchanged(self, api) -> None:
        """The bare-task default is the whole compatibility story; it stays bare."""
        client, manager = api
        blocked_band(manager)
        response = client.post(
            "/api/tasks/task-003-blocked/queue-move",
            json={"actor": "Ada", "operation_id": "op-1", "top": True},
        )
        assert response.status_code == 200
        assert response.json()["id"] == "task-003-blocked"
        assert "queue_warnings" not in response.json()

    def test_keep_records_the_anchor_over_http(self, api) -> None:
        client, manager = api
        blocked_band(manager)
        client.post(
            "/api/tasks/task-003-blocked/queue-move",
            json={"actor": "Ada", "operation_id": "op-1", "top": True},
        )
        response = client.post(
            "/api/tasks/task-003-blocked/queue-keep",
            json={"actor": "Ada", "operation_id": "op-2"},
        )
        assert response.status_code == 200
        task = manager.get_task("task-003-blocked")
        assert any(item.data.get(ANCHOR_KEY) == STRONG_ANCHOR for item in task.log)

    def test_keep_is_refused_over_http_when_nothing_was_warned(self, api) -> None:
        client, manager = api
        seed(manager, 3)
        client.post(
            "/api/tasks/task-003-work/queue-move",
            json={"actor": "Ada", "operation_id": "op-1", "top": True},
        )
        response = client.post(
            "/api/tasks/task-003-work/queue-keep",
            json={"actor": "Ada", "operation_id": "op-2"},
        )
        assert response.status_code == 409
        assert response.json()["code"] == "invalid_transition"

    def test_the_move_still_lands_when_it_warns(self, api) -> None:
        client, manager = api
        blocked_band(manager)
        client.post(
            "/api/tasks/task-003-blocked/queue-move?envelope=true",
            json={"actor": "Ada", "operation_id": "op-1", "top": True},
        )
        assert order(manager)[0] == "task-003-blocked"


class TestTheCommandLine:
    def test_queue_move_prints_the_warnings(self, project, monkeypatch) -> None:
        root, manager = project
        blocked_band(manager)
        monkeypatch.chdir(root)
        result = runner.invoke(cli_app, ["queue", "move", "task-003-blocked", "--top"])
        assert result.exit_code == 0, result.output
        assert "task-003-blocked is now high/" in result.output
        assert "cannot be claimed" in result.output
        assert "which it needs" in result.output
        assert "Undo it with: agentjobs queue move task-003-blocked --after task-002-work" in (
            result.output
        )

    def test_a_clean_move_prints_nothing_extra(self, project, monkeypatch) -> None:
        root, manager = project
        seed(manager, 3)
        monkeypatch.chdir(root)
        result = runner.invoke(
            cli_app, ["queue", "move", "task-003-work", "--before", "task-001-work"]
        )
        assert result.exit_code == 0, result.output
        assert "worth knowing" not in result.output
        assert "Undo it with" not in result.output
