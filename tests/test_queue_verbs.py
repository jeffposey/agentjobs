"""The queue as a thing that decides: assignment, the reorder verbs, and selection.

Task-205, implementing sections 5 to 9 of ``docs/task-selection-design.md``. Task-204
gave the corpus a `queue_position` field and an invariant; nothing read it. This is
where it starts to mean something, so the assertions here are about *behaviour under
change*: what happens when timestamps move, when two writers race, when a renumber is
interrupted halfway, and when the queue is broken enough that no honest answer exists.

The one to read first is :class:`TestTimestampsNoLongerDecide`. Everything else in
task-081 exists to make that test's guarantee affordable.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import pytest
import yaml

from agentjobs.manager import QueueRepairReport, TaskManager
from agentjobs.models_v2 import (
    Lifecycle,
    LogEntryType,
    MANAGER_WRITTEN_LOG_TYPES,
    Outcome,
    Priority,
)
from agentjobs.queue import (
    QUEUE_STEP,
    BandEntry,
    Placement,
    QueueCorruptionError,
    band_entries,
    plan_compaction,
    plan_insertion,
    plan_rebalance,
    plan_renumber,
)
from agentjobs.storage import TaskStorage

NOW = datetime(2026, 8, 10, tzinfo=timezone.utc)

CONFIG: Dict[str, object] = {
    "project_name": "Fixture",
    "tasks_directory": "tasks",
    "categories": ["general", "infrastructure"],
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
    yield tmp_path, TaskManager(TaskStorage(tmp_path / "tasks"))


def make(
    manager: TaskManager,
    task_id: str,
    *,
    priority: Priority = Priority.HIGH,
    lifecycle: Lifecycle = Lifecycle.READY,
    **kwargs: Any,
) -> str:
    """Create one ready task through the real verb, so it gets a real position."""
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


def band(manager: TaskManager, priority: Priority = Priority.HIGH) -> List[Tuple[str, int]]:
    """The band as it stands on disk: ids and numbers, in queue order."""
    entries = band_entries(manager.storage.list_tasks_uncached(), priority)
    return [(entry.task_id, entry.position) for entry in entries]


def order(manager: TaskManager, priority: Priority = Priority.HIGH) -> List[str]:
    return [task_id for task_id, _ in band(manager, priority)]


def touch_updated(root: Path, task_id: str, when: datetime) -> None:
    """Rewrite one task's ``updated`` stamp by hand, as a stray editor would.

    Deliberately raw. Going through a verb would be a *real* write and would stamp its
    own time; the point of the test that uses this is that the field itself has stopped
    mattering, however it got its value.
    """
    path = root / "tasks" / f"{task_id}.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["updated"] = when.isoformat().replace("+00:00", "Z")
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


def queue_moves(manager: TaskManager, task_id: str) -> List[Any]:
    task = manager.get_task(task_id)
    assert task is not None
    return [entry for entry in task.log if entry.type is LogEntryType.QUEUE_MOVE]


# ---------------------------------------------------------------------------
# sc-1 -- the test the whole program exists for
# ---------------------------------------------------------------------------


class TestTimestampsNoLongerDecide:
    """Selection must not move when `updated` moves. This is task-081's whole point.

    Before this change `get_next_task` sorted by ``priority_rank`` then ``updated``
    descending, so appending a progress note to a task promoted it above work somebody
    had deliberately put first. The failure was invisible: every answer looked
    reasonable, and the order simply was not the one anyone had chosen.
    """

    def test_rewriting_every_timestamp_changes_nothing(self, project) -> None:
        root, manager = project
        for index in range(1, 6):
            make(manager, f"task-{index:03d}-work")
        winner = manager.get_next_task()
        assert winner is not None and winner.id == "task-001-work"

        # Invert the old ranking exactly: the task that was last in line becomes the
        # most recently touched, which is what used to win.
        for offset, task_id in enumerate(order(manager)):
            touch_updated(root, task_id, NOW + timedelta(days=offset + 1))

        again = manager.get_next_task()
        assert again is not None
        assert again.id == winner.id
        assert [task.updated for task in manager.storage.list_tasks_uncached()] != []

    def test_the_old_ranking_really_would_have_changed(self, project) -> None:
        """Guard the guard: prove the rewrite above inverts what used to decide.

        Without this, a bug that made every `updated` identical would leave the test
        above passing while asserting nothing at all.
        """
        root, manager = project
        for index in range(1, 4):
            make(manager, f"task-{index:03d}-work")
        for offset, task_id in enumerate(order(manager)):
            touch_updated(root, task_id, NOW + timedelta(days=offset + 1))

        by_timestamp = sorted(
            manager.storage.list_tasks_uncached(),
            key=lambda task: (task.priority_rank(), -task.updated.timestamp()),
        )
        assert by_timestamp[0].id == "task-003-work"  # last in line, newest stamp
        assert manager.get_next_task().id == "task-001-work"

    def test_a_blocked_task_does_not_block_the_queue(self, project) -> None:
        """Claimability decides *whether*; the queue decides *which*."""
        root, manager = project
        make(manager, "task-001-epic")
        make(manager, "task-002-child", parent="task-001-epic")
        make(manager, "task-003-free")

        winner = manager.get_next_task()
        assert winner is not None
        # task-001 is first in line and has an open child, so the queue moves past it
        # to the next claimable task rather than stopping there.
        assert winner.id == "task-002-child"


# ---------------------------------------------------------------------------
# sc-2 -- concurrency, through the real lock
# ---------------------------------------------------------------------------


class TestTheQueueLockHolds:
    """Two writers computing "the bottom of the band" at once must not agree.

    Mocking the lock would test the arrangement of the code rather than the thing that
    can actually go wrong, so these run real threads against real files. The failure
    they guard is not a crash: it is two tasks quietly sharing one number.
    """

    def test_concurrent_creates_and_moves_never_duplicate_a_position(self, project) -> None:
        root, manager = project
        for index in range(1, 5):
            make(manager, f"task-{index:03d}-seed")

        errors: List[BaseException] = []
        start = threading.Barrier(8)

        def create(index: int) -> None:
            try:
                start.wait(timeout=10)
                make(manager, f"task-{100 + index:03d}-new")
            except BaseException as exc:  # pragma: no cover - reported, not swallowed
                errors.append(exc)

        def move(index: int) -> None:
            try:
                start.wait(timeout=10)
                manager.move(f"task-{index + 1:03d}-seed", top=True, actor="bot")
            except BaseException as exc:  # pragma: no cover - reported, not swallowed
                errors.append(exc)

        threads = [threading.Thread(target=create, args=(index,)) for index in range(4)]
        threads += [threading.Thread(target=move, args=(index,)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors, errors
        positions = [position for _, position in band(manager)]
        assert len(positions) == 8
        assert len(set(positions)) == 8, band(manager)
        assert manager.check_queue() == []

    def test_a_create_racing_a_move_is_serialised_by_the_queue_lock(self, project) -> None:
        """The specific race the creation lock alone did not cover.

        Creation decides two things by computing a maximum and then writing -- the next
        id and the bottom of the band. The creation lock covers the first; only the
        queue lock covers the second, and a create racing a *move* races nothing else.
        """
        root, manager = project
        make(manager, "task-001-seed")
        make(manager, "task-002-seed")

        errors: List[BaseException] = []
        start = threading.Barrier(2)

        def create() -> None:
            try:
                start.wait(timeout=10)
                make(manager, "task-003-new")
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        def shove() -> None:
            try:
                start.wait(timeout=10)
                manager.move("task-001-seed", bottom=True, actor="bot")
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=create), threading.Thread(target=shove)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors, errors
        positions = [position for _, position in band(manager)]
        assert len(set(positions)) == 3, band(manager)


# ---------------------------------------------------------------------------
# sc-3 and sc-7 -- renumbering, and the writer that does not hold the lock
# ---------------------------------------------------------------------------


class TestARenumberIsSafeToInterrupt:
    """A multi-file write here is not atomic, so every prefix of it must be valid.

    The rule from design section 6: at every instant during a renumber, the band read
    from disk is a valid queue in the same order it had before. These tests apply the
    plan one file at a time and check the band after each write, which is the only
    honest way to assert a property about states nobody normally sees.
    """

    def test_every_partial_application_is_a_correctly_ordered_queue(self, project) -> None:
        root, manager = project
        for index in range(1, 6):
            make(manager, f"task-{index:03d}-work")
        # Crowd the band so a rebalance has something to fix, and record the order the
        # renumber has to preserve.
        manager.move("task-005-work", after="task-001-work", actor="bot")
        manager.move("task-004-work", after="task-001-work", actor="bot")
        expected = order(manager)

        entries = band_entries(manager.storage.list_tasks_uncached(), Priority.HIGH)
        passes = plan_rebalance(entries)
        assert passes, "a crowded band should have something to renumber"

        for renumber_pass in passes:
            for task_id, position in renumber_pass:
                manager.apply_position(task_id, position)
                current = band(manager)
                positions = [item[1] for item in current]
                assert len(set(positions)) == len(positions), current
                assert [item[0] for item in current] == expected, current

    def test_a_two_pass_renumber_is_also_safe_at_every_step(self, project) -> None:
        """Compaction usually renumbers downward *and* upward, so it takes two passes."""
        root, manager = project
        for index in range(1, 5):
            make(manager, f"task-{index:03d}-work")
        manager.move("task-004-work", after="task-001-work", actor="bot")
        expected = order(manager)

        entries = band_entries(manager.storage.list_tasks_uncached(), Priority.HIGH)
        passes = plan_compaction(entries)
        assert len(passes) == 2, "a mixed renumber goes up into free space, then down"

        for renumber_pass in passes:
            for task_id, position in renumber_pass:
                manager.apply_position(task_id, position)
                current = band(manager)
                assert len({item[1] for item in current}) == len(current), current
                assert [item[0] for item in current] == expected, current

        assert band(manager) == [(task_id, (i + 1) * 100) for i, task_id in enumerate(expected)]

    def test_a_task_that_closes_mid_renumber_is_skipped_not_written(self, project) -> None:
        """sc-7, the concurrent-close rule.

        ``close`` takes no queue lock -- clearing a value cannot create a duplicate, and
        keeping that path cheap is deliberate. The cost lands here: a task present in
        the renumber's snapshot can close before its write arrives, and applying the
        snapshot blindly would put a position back onto a closed task, breaking rule 6
        in a file the renumber itself had just written.
        """
        root, manager = project
        for index in range(1, 5):
            make(manager, f"task-{index:03d}-work")
        entries = band_entries(manager.storage.list_tasks_uncached(), Priority.HIGH)
        passes = plan_rebalance(entries)
        planned = {task_id: position for task_id, position in passes[0]}

        # Close a task after the snapshot was taken, before its write lands.
        manager.close_task("task-002-work", actor="bot", outcome=Outcome.COMPLETED)

        for renumber_pass in passes:
            for task_id, position in renumber_pass:
                manager.apply_position(task_id, position)

        closed = manager.get_task("task-002-work")
        assert closed is not None
        assert closed.queue_position is None, "a closed task must not be given a place"
        assert planned["task-002-work"] not in [position for _, position in band(manager)]
        assert order(manager) == ["task-001-work", "task-003-work", "task-004-work"]
        assert manager.check_queue() == []

    def test_a_rebalance_only_ever_renumbers_upward(self, project) -> None:
        root, manager = project
        for index in range(1, 4):
            make(manager, f"task-{index:03d}-work")
        before = band(manager)
        manager.rebalance_band(Priority.HIGH)
        after = band(manager)
        assert [item[0] for item in after] == [item[0] for item in before]
        assert min(item[1] for item in after) > max(item[1] for item in before)

    def test_the_planner_refuses_a_target_list_that_is_not_a_queue(self) -> None:
        entries = [BandEntry("a", 100), BandEntry("b", 200)]
        with pytest.raises(ValueError, match="ascending"):
            plan_renumber(entries, [200, 100])
        with pytest.raises(ValueError, match="every member or none"):
            plan_renumber(entries, [100])


# ---------------------------------------------------------------------------
# sc-4 -- corruption is loud
# ---------------------------------------------------------------------------


def break_position(root: Path, task_id: str, position: Any) -> None:
    """Put a bad `queue_position` into a file by hand, the way a bad merge would."""
    path = root / "tasks" / f"{task_id}.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if position is None:
        raw.pop("queue_position", None)
    else:
        raw["queue_position"] = position
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


class TestABrokenQueueRefusesToAnswer:
    """Selection raises; check, list and repair still work on the same corpus.

    A queue that quietly answers while corrupt trains everybody to ignore corruption,
    and the failure it produces -- an agent silently working the wrong task -- leaves no
    trace at all. But you must be able to *see* a broken queue in order to fix it, which
    is why the reporting paths are the deliberate exception.
    """

    def test_a_duplicate_position_stops_selection_and_names_the_repair(self, project) -> None:
        root, manager = project
        make(manager, "task-001-work")
        make(manager, "task-002-work")
        break_position(root, "task-002-work", 100)

        with pytest.raises(QueueCorruptionError) as caught:
            manager.get_next_task()
        message = str(caught.value)
        assert "task-001-work" in message and "task-002-work" in message
        assert "agentjobs queue repair" in message

    def test_a_missing_position_stops_selection_too(self, project) -> None:
        """The commonest corruption, and the one a loaded-tasks check cannot see.

        Rule 6 refuses to load an open task with no position, so this file is not in
        `list_tasks()` at all. Reading the broken files raw is what makes it visible.
        """
        root, manager = project
        make(manager, "task-001-work")
        make(manager, "task-002-work")
        break_position(root, "task-002-work", None)

        with pytest.raises(QueueCorruptionError) as caught:
            manager.get_next_task()
        assert "task-002-work" in str(caught.value)
        assert "no queue_position" in str(caught.value)

    def test_a_position_below_one_stops_selection(self, project) -> None:
        root, manager = project
        make(manager, "task-001-work")
        make(manager, "task-002-work")
        break_position(root, "task-002-work", 0)
        with pytest.raises(QueueCorruptionError) as caught:
            manager.get_next_task()
        assert "task-002-work" in str(caught.value)

    def test_nothing_claimable_at_all_is_still_None_rather_than_a_raise(self, project) -> None:
        """No winning band means no claim to justify, so there is nothing to check.

        The broken file is still loudly reported -- by the loader, by `/tasks/broken`
        and by `check_queue` -- it just is not selection's business, because selection
        is not asserting anything about it.
        """
        root, manager = project
        make(manager, "task-001-work")
        break_position(root, "task-001-work", 0)
        assert manager.get_next_task() is None
        assert [problem.kind for problem in manager.check_queue()] == ["not-positive"]

    def test_corruption_below_the_winning_band_is_not_selection_s_problem(self, project) -> None:
        """Scope, from design section 8.

        A duplicate in `low` does not falsify the claim that a particular `high` task is
        next. Making every selection hostage to corruption in a band it never reads
        would punish the wrong caller -- and would mean nobody could get any work out of
        the queue until an unrelated band was tidied.
        """
        root, manager = project
        make(manager, "task-001-work", priority=Priority.HIGH)
        make(manager, "task-002-low", priority=Priority.LOW)
        make(manager, "task-003-low", priority=Priority.LOW)
        break_position(root, "task-003-low", 100)

        winner = manager.get_next_task()
        assert winner is not None and winner.id == "task-001-work"
        # Still reported, though: `check` covers every band, always.
        assert [problem.kind for problem in manager.check_queue()] == ["duplicate"]

    def test_check_and_repair_both_work_on_the_corpus_selection_refused(self, project) -> None:
        root, manager = project
        make(manager, "task-001-work")
        make(manager, "task-002-work")
        make(manager, "task-003-work")
        break_position(root, "task-002-work", 100)
        break_position(root, "task-003-work", None)

        problems = manager.check_queue()
        assert {problem.kind for problem in problems} == {"duplicate", "missing"}

        report = manager.repair_queue()
        assert isinstance(report, QueueRepairReport)
        assert report.changed
        assert manager.check_queue() == []
        winner = manager.get_next_task()
        assert winner is not None and winner.id == "task-001-work"

    def test_repair_is_deterministic_and_names_what_it_guessed(self, project) -> None:
        root, manager = project
        make(manager, "task-001-work")
        make(manager, "task-002-work")
        break_position(root, "task-002-work", 100)

        report = manager.repair_queue()
        assert [item.task_id for item in report.assigned] == ["task-002-work"]
        assert "task-002-work" in report.render()
        # The earlier-created task keeps the contested number; the loser goes to the
        # bottom. Both halves of that rule are immutable, so it is reproducible.
        assert order(manager) == ["task-001-work", "task-002-work"]

    def test_repair_leaves_a_healthy_corpus_alone(self, project) -> None:
        root, manager = project
        make(manager, "task-001-work")
        make(manager, "task-002-work")
        before = band(manager)
        report = manager.repair_queue()
        assert not report.changed
        assert band(manager) == before

    def test_nothing_falls_back_to_a_timestamp_when_the_queue_is_broken(self, project) -> None:
        """The constraint, stated as a test: refusing *is* the behaviour.

        A fallback ordering would be the worst of both worlds -- an answer nobody chose,
        delivered with no sign that the queue had stopped working.
        """
        root, manager = project
        make(manager, "task-001-work")
        make(manager, "task-002-work")
        break_position(root, "task-002-work", 100)
        with pytest.raises(QueueCorruptionError):
            manager.get_next_task()
        with pytest.raises(QueueCorruptionError):
            manager.explain_next()


# ---------------------------------------------------------------------------
# sc-5 -- the verbs, their records, and their replays
# ---------------------------------------------------------------------------


class TestTheReorderVerbs:
    """`move` and `reprioritize` are decisions, so each leaves one entry saying so."""

    def test_a_move_writes_exactly_one_file(self, project) -> None:
        """The load-bearing property of sparse numbering.

        If a move is rewriting the band, the numbering has been implemented wrong: in a
        git-backed, one-file-per-task corpus worked by several agents at once, that is
        the difference between a one-line diff and a diff that conflicts with everything
        in flight.
        """
        root, manager = project
        for index in range(1, 6):
            make(manager, f"task-{index:03d}-work")
        stamps = {path.name: path.read_bytes() for path in (root / "tasks").glob("*.yaml")}

        manager.move("task-005-work", top=True, actor="Ada")

        changed = [
            name
            for name, content in stamps.items()
            if (root / "tasks" / name).read_bytes() != content
        ]
        assert changed == ["task-005-work.yaml"], changed

    def test_a_move_records_the_decision(self, project) -> None:
        root, manager = project
        make(manager, "task-001-work")
        make(manager, "task-002-work")
        before = dict(band(manager))

        manager.move("task-002-work", before="task-001-work", actor="Ada")

        entries = queue_moves(manager, "task-002-work")
        assert len(entries) == 1
        data = entries[0].data
        assert data["band"] == "high"
        assert data["from"] == before["task-002-work"]
        assert data["to"] == dict(band(manager))["task-002-work"]
        assert data["placement"] == {"kind": "before", "target": "task-001-work"}
        assert order(manager) == ["task-002-work", "task-001-work"]

    def test_a_move_replays_rather_than_moving_twice(self, project) -> None:
        root, manager = project
        make(manager, "task-001-work")
        make(manager, "task-002-work")
        make(manager, "task-003-work")

        first = manager.move("task-003-work", top=True, actor="Ada", operation_id="op-move-1")
        again = manager.move("task-003-work", top=True, actor="Ada", operation_id="op-move-1")
        assert again.queue_position == first.queue_position
        assert len(queue_moves(manager, "task-003-work")) == 1
        assert manager.check_queue() == []

    def test_a_move_rebalances_when_the_gap_runs_out(self, project) -> None:
        """Sparse numbering runs out after about six inserts in one spot. It retries."""
        root, manager = project
        make(manager, "task-001-work")
        make(manager, "task-002-work")
        for index in range(3, 12):
            make(manager, f"task-{index:03d}-work")
            manager.move(f"task-{index:03d}-work", after="task-001-work", actor="Ada")
        current = order(manager)
        assert current[0] == "task-001-work"
        assert current[1] == "task-011-work"
        assert manager.check_queue() == []

    def test_before_and_after_refuse_a_task_in_another_band(self, project) -> None:
        root, manager = project
        make(manager, "task-001-work", priority=Priority.HIGH)
        make(manager, "task-002-slow", priority=Priority.LOW)
        with pytest.raises(ValueError, match="same band"):
            manager.move("task-001-work", before="task-002-slow", actor="Ada")

    def test_exactly_one_placement_is_required(self, project) -> None:
        root, manager = project
        make(manager, "task-001-work")
        with pytest.raises(ValueError, match="exactly one placement"):
            manager.move("task-001-work", actor="Ada")
        with pytest.raises(ValueError, match="exactly one placement"):
            manager.move("task-001-work", top=True, bottom=True, actor="Ada")

    def test_a_group_move_carries_only_the_children_in_the_same_band(self, project) -> None:
        """Contiguity is a within-band property (design section 5.2).

        Positions in different bands are never compared, so "next to its parent" has no
        meaning for a `medium` child of a `high` epic. `moved_with` names exactly who
        moved, so the record also shows who stayed where they were.
        """
        root, manager = project
        make(manager, "task-001-first")
        make(manager, "task-002-epic")
        make(manager, "task-003-child", parent="task-002-epic")
        make(manager, "task-004-elsewhere", parent="task-002-epic", priority=Priority.LOW)
        low_before = band(manager, Priority.LOW)

        manager.move("task-002-epic", top=True, with_children=True, actor="Ada")

        assert order(manager)[:2] == ["task-002-epic", "task-003-child"]
        entry = queue_moves(manager, "task-002-epic")[0]
        assert entry.data["moved_with"] == ["task-003-child"]
        assert band(manager, Priority.LOW) == low_before

    def test_a_group_move_carries_grandchildren_too(self, project) -> None:
        root, manager = project
        make(manager, "task-001-first")
        make(manager, "task-002-epic")
        make(manager, "task-003-child", parent="task-002-epic")
        make(manager, "task-004-grandchild", parent="task-003-child")

        manager.move("task-002-epic", top=True, with_children=True, actor="Ada")
        assert order(manager)[:3] == [
            "task-002-epic",
            "task-003-child",
            "task-004-grandchild",
        ]

    def test_a_move_cannot_be_placed_relative_to_something_it_is_moving(self, project) -> None:
        root, manager = project
        make(manager, "task-001-epic")
        make(manager, "task-002-child", parent="task-001-epic")
        with pytest.raises(ValueError, match="being moved"):
            manager.move(
                "task-001-epic",
                before="task-002-child",
                with_children=True,
                actor="Ada",
            )

    def test_reprioritize_changes_band_and_place_together(self, project) -> None:
        root, manager = project
        make(manager, "task-001-high", priority=Priority.HIGH)
        make(manager, "task-002-slow", priority=Priority.LOW)
        make(manager, "task-003-slow", priority=Priority.LOW)

        moved = manager.reprioritize("task-003-slow", Priority.HIGH, actor="Ada")

        assert moved.priority is Priority.HIGH
        assert order(manager, Priority.HIGH) == ["task-001-high", "task-003-slow"]
        assert order(manager, Priority.LOW) == ["task-002-slow"]
        entry = queue_moves(manager, "task-003-slow")[0]
        assert entry.data["band"] == "high"
        assert entry.data["from_band"] == "low"
        assert entry.data["placement"] == {"kind": "bottom"}

    def test_reprioritize_defaults_to_the_bottom_of_the_new_band(self, project) -> None:
        """A band change already says everything about urgency.

        Where inside the new band it lands is a separate question, and "bottom" is the
        answer that assumes least about what the caller meant.
        """
        root, manager = project
        make(manager, "task-001-high")
        make(manager, "task-002-high")
        make(manager, "task-003-slow", priority=Priority.LOW)
        manager.reprioritize("task-003-slow", Priority.HIGH, actor="Ada")
        assert order(manager)[-1] == "task-003-slow"

    def test_reprioritize_takes_an_explicit_placement(self, project) -> None:
        root, manager = project
        make(manager, "task-001-high")
        make(manager, "task-002-slow", priority=Priority.LOW)
        manager.reprioritize("task-002-slow", Priority.HIGH, top=True, actor="Ada")
        assert order(manager) == ["task-002-slow", "task-001-high"]

    def test_reprioritize_replays(self, project) -> None:
        root, manager = project
        make(manager, "task-001-high")
        make(manager, "task-002-slow", priority=Priority.LOW)
        first = manager.reprioritize(
            "task-002-slow", Priority.HIGH, actor="Ada", operation_id="op-repri-1"
        )
        again = manager.reprioritize(
            "task-002-slow", Priority.HIGH, actor="Ada", operation_id="op-repri-1"
        )
        assert again.queue_position == first.queue_position
        assert len(queue_moves(manager, "task-002-slow")) == 1

    def test_a_priority_patch_is_intercepted_and_keeps_the_queue_valid(self, project) -> None:
        """`priority` is an ordinary content field to every existing caller.

        A band change that carried its old number across would put two tasks on one
        position, with nothing in the record to say which came first -- so the manager
        routes the patch through the same placement `reprioritize` uses.
        """
        root, manager = project
        make(manager, "task-001-high")
        make(manager, "task-002-slow", priority=Priority.LOW)
        assert dict(band(manager, Priority.LOW))["task-002-slow"] == QUEUE_STEP

        manager.update_task("task-002-slow", actor="Ada", priority=Priority.HIGH)

        assert manager.check_queue() == []
        assert order(manager) == ["task-001-high", "task-002-slow"]
        assert queue_moves(manager, "task-002-slow")[0].data["from_band"] == "low"

    def test_a_patch_that_does_not_change_the_band_records_no_move(self, project) -> None:
        root, manager = project
        make(manager, "task-001-high")
        manager.update_task("task-001-high", actor="Ada", priority=Priority.HIGH)
        manager.update_task("task-001-high", actor="Ada", title="Renamed")
        assert queue_moves(manager, "task-001-high") == []

    def test_a_reopened_task_re_enters_at_the_bottom_of_its_band(self, project) -> None:
        """It does not remember where it used to be, and should not.

        The queue moved on without it (design section 5.4). There is no `reopen` verb
        yet, so the generic patch is the only path that can produce one -- and it is the
        path that has to hold the queue lock while it assigns.
        """
        root, manager = project
        make(manager, "task-001-work")
        make(manager, "task-002-work")
        make(manager, "task-003-work")
        manager.move("task-003-work", top=True, actor="Ada")
        manager.close_task("task-003-work", actor="Ada", outcome=Outcome.COMPLETED)
        assert manager.get_task("task-003-work").queue_position is None

        manager.update_task(
            "task-003-work",
            actor="Ada",
            lifecycle=Lifecycle.READY,
            outcome=None,
            ball="agent",
            ball_reason="available",
        )

        assert order(manager)[-1] == "task-003-work"
        assert manager.check_queue() == []
        assert queue_moves(manager, "task-003-work")[-1].body.startswith("Reopened")

    def test_close_clears_the_position_and_takes_no_queue_lock(self, project) -> None:
        root, manager = project
        make(manager, "task-001-work")
        with manager.storage.queue_lock():
            # Deliberately inside the lock: close must not need it. If it did, this
            # would deadlock rather than fail, so the timeout is the assertion.
            closed = manager.close_task("task-001-work", actor="Ada", outcome=Outcome.COMPLETED)
        assert closed.queue_position is None

    def test_a_created_placement_writes_one_queue_move_record(self, project) -> None:
        root, manager = project
        make(manager, "task-001-work")
        make(manager, "task-002-work")
        manager.create_task(
            id="task-003-urgent",
            title="Urgent",
            description="Body.",
            priority=Priority.HIGH,
            lifecycle=Lifecycle.READY,
            actor="Ada",
            placement=Placement(Placement.TOP),
        )
        assert order(manager)[0] == "task-003-urgent"
        entries = queue_moves(manager, "task-003-urgent")
        assert len(entries) == 1
        assert entries[0].data["from"] is None
        assert entries[0].data["placement"] == {"kind": "top"}
        assert manager.check_queue() == []

    def test_an_ordinary_create_goes_to_the_bottom_and_records_nothing(self, project) -> None:
        """The default is not a decision, so it does not get an entry.

        An entry on every create saying "it went last" would bury the ones that mean
        something, which is the same argument that keeps rebalances out of the log.
        """
        root, manager = project
        make(manager, "task-001-work")
        make(manager, "task-002-work")
        assert order(manager) == ["task-001-work", "task-002-work"]
        assert queue_moves(manager, "task-002-work") == []

    def test_a_created_placement_refuses_a_target_in_another_band(self, project) -> None:
        root, manager = project
        make(manager, "task-001-slow", priority=Priority.LOW)
        with pytest.raises(ValueError, match="same band"):
            manager.create_task(
                id="task-002-work",
                title="Work",
                description="Body.",
                priority=Priority.HIGH,
                lifecycle=Lifecycle.READY,
                actor="Ada",
                placement=Placement(Placement.BEFORE, "task-001-slow"),
            )

    def test_a_caller_may_not_forge_a_queue_move_entry(self, project) -> None:
        """`queue_move` asserts that something happened, so the manager owns it."""
        root, manager = project
        make(manager, "task-001-work")
        assert LogEntryType.QUEUE_MOVE in MANAGER_WRITTEN_LOG_TYPES
        with pytest.raises(ValueError):
            manager.add_log_entry(
                "task-001-work",
                actor="Ada",
                type=LogEntryType.QUEUE_MOVE,
                body="I moved it, honest.",
            )

    def test_a_rebalance_records_no_decision(self, project) -> None:
        """Nobody decided anything, and forty entries saying so would bury the rest."""
        root, manager = project
        for index in range(1, 4):
            make(manager, f"task-{index:03d}-work")
        manager.rebalance_band(Priority.HIGH)
        manager.compact_band(Priority.HIGH)
        for index in range(1, 4):
            assert queue_moves(manager, f"task-{index:03d}-work") == []


# ---------------------------------------------------------------------------
# sc-6 -- the scheduler explains itself
# ---------------------------------------------------------------------------


class TestTheQueueExplainsItself:
    """ "Why not the one I was expecting?" is the question a human actually asks."""

    def test_it_names_the_winner_s_band_and_position(self, project) -> None:
        root, manager = project
        make(manager, "task-001-work")
        explanation = manager.explain_next()
        assert explanation.task == "task-001-work"
        assert explanation.band == "high"
        assert explanation.queue_position == QUEUE_STEP
        assert explanation.skipped == ()

    def test_it_lists_what_was_skipped_with_the_rule_that_excluded_it(self, project) -> None:
        root, manager = project
        make(manager, "task-001-epic")
        make(manager, "task-002-child", parent="task-001-epic")
        make(manager, "task-003-blocked")
        make(manager, "task-004-free")
        manager.update_task(
            "task-003-blocked",
            actor="Ada",
            dependencies=[{"task": "task-004-free", "type": "needs"}],
        )
        manager.claim_task("task-002-child", agent="bot")

        explanation = manager.explain_next()
        assert explanation.task == "task-003-blocked" or explanation.task == "task-004-free"
        reasons = {item.task: item.reason for item in explanation.skipped}
        assert reasons["task-001-epic"] == "has 1 open child"
        assert reasons["task-002-child"] == "not ready (active, held by agent)"
        assert all(item.queue_position is not None for item in explanation.skipped)

    def test_it_names_empty_bands_above_the_winner(self, project) -> None:
        root, manager = project
        make(manager, "task-001-work", priority=Priority.MEDIUM)
        explanation = manager.explain_next()
        assert explanation.band == "medium"
        assert explanation.empty_bands_above == ("critical", "high")

    def test_a_dependency_names_what_it_is_waiting_on(self, project) -> None:
        root, manager = project
        make(manager, "task-001-blocked")
        make(manager, "task-002-blocker")
        manager.update_task(
            "task-001-blocked",
            actor="Ada",
            dependencies=[{"task": "task-002-blocker", "type": "needs"}],
        )
        explanation = manager.explain_next()
        assert explanation.task == "task-002-blocker"
        assert explanation.skipped[0].task == "task-001-blocked"
        assert explanation.skipped[0].reason.startswith("waiting on task-002-blocker")

    def test_with_nothing_claimable_every_open_task_is_listed(self, project) -> None:
        """The listing a reader wants precisely when a tool says there is nothing to do."""
        root, manager = project
        make(manager, "task-001-draft", lifecycle=Lifecycle.DRAFT)
        explanation = manager.explain_next()
        assert explanation.task is None
        assert [item.task for item in explanation.skipped] == ["task-001-draft"]
        assert explanation.skipped[0].reason.startswith("not ready (draft")

    def test_the_structure_serialises_as_design_section_9_describes(self, project) -> None:
        root, manager = project
        make(manager, "task-001-epic")
        make(manager, "task-002-child", parent="task-001-epic")
        payload = manager.explain_next().as_dict()
        assert set(payload) == {
            "task",
            "band",
            "queue_position",
            "empty_bands_above",
            "skipped",
        }
        assert payload["skipped"][0] == {
            "task": "task-001-epic",
            "position": QUEUE_STEP,
            "reason": "has 1 open child",
        }

    def test_an_agent_filter_reports_the_restriction(self, project) -> None:
        root, manager = project
        make(manager, "task-001-reserved", assignment={"eligible": ["codex"]})
        make(manager, "task-002-free")
        explanation = manager.explain_next(agent="bot")
        assert explanation.task == "task-002-free"
        assert explanation.skipped[0].reason == "restricted to codex"


# ---------------------------------------------------------------------------
# The arithmetic, on its own
# ---------------------------------------------------------------------------


class TestTheInsertionArithmetic:
    """Sparse integers: readable, typeable, and a one-file diff per move."""

    def test_an_insert_takes_the_midpoint(self) -> None:
        entries = [BandEntry("a", 100), BandEntry("b", 200)]
        assert plan_insertion(entries, Placement(Placement.BEFORE, "b")) == [150]
        assert plan_insertion(entries, Placement(Placement.AFTER, "a")) == [150]

    def test_the_top_of_a_band_halves_the_first_number(self) -> None:
        assert plan_insertion([BandEntry("a", 100)], Placement(Placement.TOP)) == [50]

    def test_the_bottom_of_a_band_adds_a_step(self) -> None:
        entries = [BandEntry("a", 100), BandEntry("b", 200)]
        assert plan_insertion(entries, Placement(Placement.BOTTOM)) == [300]

    def test_an_empty_band_starts_at_one_step(self) -> None:
        assert plan_insertion([], Placement(Placement.BOTTOM)) == [QUEUE_STEP]
        assert plan_insertion([], Placement(Placement.TOP)) == [QUEUE_STEP]

    def test_an_exhausted_gap_reports_itself_rather_than_colliding(self) -> None:
        entries = [BandEntry("a", 100), BandEntry("b", 101)]
        assert plan_insertion(entries, Placement(Placement.BEFORE, "b")) is None

    def test_a_group_lands_contiguously_and_in_order(self) -> None:
        entries = [BandEntry("a", 100), BandEntry("b", 400)]
        assert plan_insertion(entries, Placement(Placement.AFTER, "a"), count=2) == [200, 300]
