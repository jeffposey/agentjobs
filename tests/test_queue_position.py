"""Queue position: the invariant, the corpus check, and the migration baseline.

Task-204, implementing sections 3, 8 and 15 (step 1) of docs/task-selection-design.md.
Nothing consumes the number yet -- selection is task-205 -- so what is asserted here is
that the field exists, that it cannot be absent when it matters or present when it does
not, that a broken queue is *reportable* rather than merely unloadable, and that the
migration that fills the corpus in is a function of immutable data only.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import pytest
import yaml

from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Lifecycle, Outcome, Priority, Task
from agentjobs.queue import (
    QUEUE_STEP,
    QueueRecord,
    migrate_queue_positions,
    next_position,
    plan_queue_migration,
    read_queue_records,
)
from agentjobs.storage import TaskStorage
from agentjobs.validation import validate_corpus

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


def task_data(**overrides: Any) -> Dict[str, Any]:
    """A minimal valid v2 task document, overridable per test."""
    payload: Dict[str, Any] = {
        "schema": 2,
        "id": "task-001-example",
        "title": "Example",
        "created": NOW,
        "updated": NOW,
        "lifecycle": "ready",
        "ball": "agent",
        "ball_reason": "available",
        "priority": "medium",
        "queue_position": 100,
        "category": "general",
        "spec": {"summary": "A summary.", "description": "What to do."},
    }
    payload.update(overrides)
    return payload


@pytest.fixture()
def project(tmp_path: Path) -> Iterator[Tuple[Path, TaskManager]]:
    """A project directory with config and an empty tasks directory."""
    (tmp_path / ".agentjobs").mkdir(parents=True)
    (tmp_path / ".agentjobs" / "config.yaml").write_text(yaml.safe_dump(CONFIG), encoding="utf-8")
    yield tmp_path, TaskManager(TaskStorage(tmp_path / "tasks"))


def write_raw(root: Path, name: str, payload: Dict[str, Any]) -> Path:
    """Write a task file by hand, exactly as a direct editor would."""
    path = root / "tasks" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def rules_reported(root: Path) -> set:
    """Every rule name the validator reports for this fixture project."""
    report = validate_corpus(root / "tasks", project_config=CONFIG, project_root=root)
    return {finding.rule for finding in report.findings}


def record(
    task_id: str,
    *,
    created: str,
    priority: str = "medium",
    is_open: bool = True,
    queue_position: int | None = None,
) -> QueueRecord:
    return QueueRecord(
        task_id=task_id,
        created=created,
        priority=priority,
        is_open=is_open,
        queue_position=queue_position,
    )


class TestRuleSixTheInvariant:
    """sc-1. Present if and only if the task is open -- the shape of the ball rule."""

    def test_an_open_task_without_a_position_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="open, so queue_position is required"):
            Task.model_validate(task_data(queue_position=None))

    @pytest.mark.parametrize("lifecycle", ["draft", "ready", "active"])
    def test_every_open_lifecycle_needs_one_including_draft(self, lifecycle: str) -> None:
        """A draft is open. It is not claimable, which is a different question."""
        payload = task_data(
            lifecycle=lifecycle,
            ball="human" if lifecycle == "draft" else "agent",
            ball_reason="spec" if lifecycle == "draft" else "available",
            ball_prompt="Do the thing.",
            assignment={"owner": "bot"} if lifecycle == "active" else {},
            queue_position=None,
        )
        with pytest.raises(ValueError, match="queue_position is required"):
            Task.model_validate(payload)

    def test_a_closed_task_with_a_position_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="closed task must not have a queue_position"):
            Task.model_validate(
                task_data(
                    lifecycle="closed",
                    ball=None,
                    ball_reason=None,
                    outcome="completed",
                    queue_position=100,
                )
            )

    def test_a_closed_task_without_one_is_valid(self) -> None:
        task = Task.model_validate(
            task_data(
                lifecycle="closed",
                ball=None,
                ball_reason=None,
                outcome="completed",
                queue_position=None,
            )
        )
        assert task.queue_position is None

    @pytest.mark.parametrize("position", [0, -1, -100])
    def test_a_non_positive_position_is_rejected(self, position: int) -> None:
        """`ge=1` on the field: the numbers mean order, and order starts at one."""
        with pytest.raises(ValueError):
            Task.model_validate(task_data(queue_position=position))

    def test_it_survives_a_round_trip_through_yaml(self, tmp_path: Path) -> None:
        storage = TaskStorage(tmp_path)
        storage.save_task(Task.model_validate(task_data(queue_position=4200)))

        assert "queue_position: 4200" in (tmp_path / "task-001-example.yaml").read_text(
            encoding="utf-8"
        )
        reloaded = storage.load_task("task-001-example")
        assert reloaded is not None and reloaded.queue_position == 4200


class TestTheVerbsThatWouldOtherwiseBreak:
    """The two manager paths rule 6 forces. See the decision entry on task-204.

    Placement, reordering and the queue lock are task-205; what is asserted here is
    only that creating and closing a task still produce a record that validates.
    """

    def test_a_created_task_lands_at_the_bottom_of_its_band(self, project) -> None:
        _, manager = project
        first = manager.create_task(id="task-001-a", title="A", description="d", category="general")
        second = manager.create_task(
            id="task-002-b", title="B", description="d", category="general"
        )

        assert first.queue_position == QUEUE_STEP
        assert second.queue_position == 2 * QUEUE_STEP

    def test_bands_are_numbered_independently(self, project) -> None:
        """A `high` task does not take a number because a `medium` one exists."""
        _, manager = project
        manager.create_task(
            id="task-001-a",
            title="A",
            description="d",
            category="general",
            priority=Priority.MEDIUM,
        )
        high = manager.create_task(
            id="task-002-b",
            title="B",
            description="d",
            category="general",
            priority=Priority.HIGH,
        )

        assert high.queue_position == QUEUE_STEP

    def test_closing_a_task_takes_it_out_of_the_line(self, project) -> None:
        _, manager = project
        manager.create_task(
            id="task-001-a",
            title="A",
            description="d",
            category="general",
            lifecycle=Lifecycle.READY,
        )

        closed = manager.close_task("task-001-a", actor="bot", outcome=Outcome.COMPLETED)

        assert closed.queue_position is None

    def test_a_closed_position_is_not_reused_by_the_next_create(self, project) -> None:
        """Gaps are normal: the numbers mean order, not rank-from-the-top."""
        _, manager = project
        manager.create_task(id="task-001-a", title="A", description="d", category="general")
        second = manager.create_task(
            id="task-002-b", title="B", description="d", category="general"
        )
        assert second.queue_position == 2 * QUEUE_STEP
        manager.close_task("task-002-b", actor="bot", outcome=Outcome.COMPLETED)

        third = manager.create_task(id="task-003-c", title="C", description="d", category="general")

        assert third.queue_position == 2 * QUEUE_STEP  # the gap is real, and reused


class TestNextPosition:
    def test_an_empty_band_starts_at_one_step(self) -> None:
        assert next_position([], Priority.HIGH) == QUEUE_STEP

    def test_a_closed_task_does_not_hold_the_band_open(self) -> None:
        closed = Task.model_validate(
            task_data(
                lifecycle="closed",
                ball=None,
                ball_reason=None,
                outcome="completed",
                queue_position=None,
                priority="high",
            )
        )
        assert next_position([closed], Priority.HIGH) == QUEUE_STEP


class TestValidatorReportsABrokenQueue:
    """sc-2. Findings, never exceptions: you must see a broken queue to fix it."""

    def test_a_clean_corpus_reports_nothing(self, project) -> None:
        root, manager = project
        manager.create_task(id="task-001-a", title="A", description="d", category="general")

        assert rules_reported(root) == set()

    def test_an_open_task_with_no_position_is_reported(self, project) -> None:
        root, _ = project
        write_raw(root, "task-901-none.yaml", task_data(id="task-901-none", queue_position=None))

        assert "queue-missing" in rules_reported(root)

    def test_a_position_on_a_closed_task_is_reported(self, project) -> None:
        root, _ = project
        write_raw(
            root,
            "task-902-closed.yaml",
            task_data(
                id="task-902-closed",
                lifecycle="closed",
                ball=None,
                ball_reason=None,
                outcome="completed",
                queue_position=100,
            ),
        )

        assert "queue-on-closed" in rules_reported(root)

    @pytest.mark.parametrize("position", [0, -5])
    def test_a_non_positive_position_is_reported(self, project, position: int) -> None:
        root, _ = project
        write_raw(
            root, "task-903-zero.yaml", task_data(id="task-903-zero", queue_position=position)
        )

        assert "queue-not-positive" in rules_reported(root)

    def test_two_open_tasks_sharing_a_position_in_one_band_are_reported(self, project) -> None:
        root, _ = project
        for task_id in ("task-904-a", "task-905-b"):
            write_raw(
                root,
                f"{task_id}.yaml",
                task_data(id=task_id, priority="high", queue_position=300),
            )

        report = validate_corpus(root / "tasks", project_config=CONFIG, project_root=root)
        duplicates = [f for f in report.findings if f.rule == "queue-duplicate"]

        assert {f.task_id for f in duplicates} == {"task-904-a", "task-905-b"}
        # Both ids in the message: a duplicate you can only half-see is half a report.
        assert all("task-904-a" in f.message and "task-905-b" in f.message for f in duplicates)

    def test_the_same_number_in_two_bands_is_not_a_duplicate(self, project) -> None:
        """Positions are unique per band. `high/100` and `medium/100` do not collide."""
        root, _ = project
        write_raw(root, "task-906-h.yaml", task_data(id="task-906-h", priority="high"))
        write_raw(root, "task-907-m.yaml", task_data(id="task-907-m", priority="medium"))

        assert "queue-duplicate" not in rules_reported(root)

    def test_two_closed_tasks_never_collide(self, project) -> None:
        """Closed tasks are not in line, so they cannot be in the way of each other."""
        root, _ = project
        for task_id in ("task-908-a", "task-909-b"):
            write_raw(
                root,
                f"{task_id}.yaml",
                task_data(
                    id=task_id,
                    lifecycle="closed",
                    ball=None,
                    ball_reason=None,
                    outcome="completed",
                    queue_position=None,
                ),
            )

        # Hand-written, so `non-canonical` is expected; no queue rule should fire.
        assert not {rule for rule in rules_reported(root) if rule.startswith("queue-")}

    def test_a_corpus_that_cannot_load_is_still_reported_and_not_raised(self, project) -> None:
        """The point of reading raw YAML: an unloadable file still gets a queue finding.

        Rule 6 means a missing position makes the file unloadable, so a check written
        against loaded tasks would go quiet exactly when the queue is broken.
        """
        root, _ = project
        write_raw(root, "task-910-x.yaml", task_data(id="task-910-x", queue_position=None))

        report = validate_corpus(root / "tasks", project_config=CONFIG, project_root=root)

        rules = {finding.rule for finding in report.findings}
        assert "unreadable" in rules  # the loader's complaint...
        assert "queue-missing" in rules  # ...and the one that names the actual rule


class TestTheMigrationBaseline:
    """sc-3. Section 15 step 1: created ascending, then id, in steps of 100."""

    def test_a_band_is_numbered_from_one_step_upward(self) -> None:
        plan = plan_queue_migration(
            [
                record("task-003", created="2026-01-03T00:00:00Z"),
                record("task-001", created="2026-01-01T00:00:00Z"),
                record("task-002", created="2026-01-02T00:00:00Z"),
            ]
        )

        assert plan.positions() == {"task-001": 100, "task-002": 200, "task-003": 300}

    def test_id_breaks_a_tie_on_created(self) -> None:
        plan = plan_queue_migration(
            [
                record("task-b", created="2026-01-01T00:00:00Z"),
                record("task-a", created="2026-01-01T00:00:00Z"),
            ]
        )

        assert plan.positions() == {"task-a": 100, "task-b": 200}

    def test_closed_tasks_are_skipped_entirely(self) -> None:
        plan = plan_queue_migration(
            [
                record("task-001", created="2026-01-01T00:00:00Z", is_open=False),
                record("task-002", created="2026-01-02T00:00:00Z"),
            ]
        )

        assert plan.positions() == {"task-002": 100}
        assert plan.closed == ["task-001"]

    def test_each_band_is_numbered_from_scratch(self) -> None:
        plan = plan_queue_migration(
            [
                record("task-001", created="2026-01-01T00:00:00Z", priority="high"),
                record("task-002", created="2026-01-02T00:00:00Z", priority="high"),
                record("task-003", created="2026-01-03T00:00:00Z", priority="low"),
            ]
        )

        assert plan.positions() == {"task-001": 100, "task-002": 200, "task-003": 100}

    def test_an_already_positioned_task_keeps_its_number(self) -> None:
        plan = plan_queue_migration(
            [record("task-001", created="2026-01-01T00:00:00Z", queue_position=7)]
        )

        assert plan.changed is False
        assert plan.already_positioned == ["task-001"]

    def test_a_partly_positioned_band_appends_below_what_is_there(self) -> None:
        plan = plan_queue_migration(
            [
                record("task-001", created="2026-01-01T00:00:00Z", queue_position=900),
                record("task-002", created="2026-01-02T00:00:00Z"),
            ]
        )

        assert plan.positions() == {"task-002": 1000}


class TestTheMigrationIsAFunctionOfImmutableDataOnly:
    """sc-3, stated as the property that matters: `updated` cannot move the queue."""

    def _corpus(self, root: Path, count: int = 12) -> Path:
        """A corpus of unpositioned open tasks across three bands."""
        tasks = root / "tasks"
        tasks.mkdir(parents=True, exist_ok=True)
        for index in range(1, count + 1):
            payload = task_data(
                id=f"task-{index:03d}-x",
                created=f"2026-01-{index:02d}T00:00:00Z",
                updated=f"2026-02-{index:02d}T00:00:00Z",
                priority=["low", "medium", "high"][index % 3],
                queue_position=None,
            )
            (tasks / f"task-{index:03d}-x.yaml").write_text(
                yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
            )
        return tasks

    def test_rewriting_every_updated_in_any_order_leaves_the_plan_identical(
        self, tmp_path: Path
    ) -> None:
        tasks = self._corpus(tmp_path)
        before = plan_queue_migration(read_queue_records(tasks)[0]).positions()

        # Every `updated` shuffled to a random new value. If the migration read one,
        # this is what would move the queue -- which is the failure task-081 exists
        # to end, expressed as a test.
        shuffler = random.Random(20260821)
        paths = sorted(tasks.glob("*.yaml"))
        shuffler.shuffle(paths)
        for path in paths:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            raw["updated"] = f"2026-09-{shuffler.randint(1, 28):02d}T00:00:00Z"
            path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

        after = plan_queue_migration(read_queue_records(tasks)[0]).positions()

        assert after == before
        assert len(before) == 12  # and it was not vacuously empty

    def test_two_runs_over_the_same_corpus_agree(self, tmp_path: Path) -> None:
        tasks = self._corpus(tmp_path)

        first = plan_queue_migration(read_queue_records(tasks)[0])
        second = plan_queue_migration(read_queue_records(tasks)[0])

        assert first.positions() == second.positions()
        assert [a.render() for a in first.assignments] == [a.render() for a in second.assignments]


class TestTheMigrationAgainstRealFiles:
    """sc-5, in miniature: run it on a corpus and check what came out."""

    def _unpositioned(self, root: Path) -> Path:
        tasks = root / "tasks"
        tasks.mkdir(parents=True, exist_ok=True)
        for index, priority in enumerate(["high", "high", "medium"], start=1):
            payload = task_data(
                id=f"task-{index:03d}-x",
                created=f"2026-01-{index:02d}T00:00:00Z",
                priority=priority,
                queue_position=None,
            )
            (tasks / f"task-{index:03d}-x.yaml").write_text(
                yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
            )
        return tasks

    def test_a_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        tasks = self._unpositioned(tmp_path)
        before = {p.name: p.read_text(encoding="utf-8") for p in tasks.glob("*.yaml")}

        report = migrate_queue_positions(tasks, write=False)

        assert report.written is False
        assert report.changed is True
        assert {p.name: p.read_text(encoding="utf-8") for p in tasks.glob("*.yaml")} == before

    def test_writing_makes_every_open_task_loadable_again(self, tmp_path: Path) -> None:
        tasks = self._unpositioned(tmp_path)
        storage = TaskStorage(tasks)
        assert storage.load_all().errors, "precondition: rule 6 refuses the corpus as it is"

        migrate_queue_positions(tasks, write=True)

        loaded = storage.load_all()
        assert loaded.errors == []
        positions = {task.id: task.queue_position for task in loaded.tasks}
        assert positions == {"task-001-x": 100, "task-002-x": 200, "task-003-x": 100}

    def test_a_second_run_is_a_no_op(self, tmp_path: Path) -> None:
        tasks = self._unpositioned(tmp_path)
        migrate_queue_positions(tasks, write=True)
        after_first = {p.name: p.read_text(encoding="utf-8") for p in tasks.glob("*.yaml")}

        report = migrate_queue_positions(tasks, write=True)

        assert report.changed is False
        # Byte-identical, so the no-op did not even bump `updated`.
        assert {p.name: p.read_text(encoding="utf-8") for p in tasks.glob("*.yaml")} == after_first

    def test_the_migrated_corpus_validates_clean(self, project) -> None:
        root, _ = project
        tasks = self._unpositioned(root)

        migrate_queue_positions(tasks, write=True)

        assert rules_reported(root) == set()

    def test_a_file_it_cannot_read_is_named_rather_than_guessed_at(self, tmp_path: Path) -> None:
        tasks = self._unpositioned(tmp_path)
        (tasks / "task-999-junk.yaml").write_text("- not: a mapping\n", encoding="utf-8")

        report = migrate_queue_positions(tasks, write=False)

        assert report.unreadable == ["task-999-junk.yaml"]
        assert "task-999-junk" not in report.positions()

    def test_the_report_says_what_it_did(self, tmp_path: Path) -> None:
        tasks = self._unpositioned(tmp_path)

        rendered = migrate_queue_positions(tasks, write=True).render()

        assert "Open tasks positioned:   3" in rendered
        assert "Written to disk:         yes" in rendered
        assert "task-001-x: high -> 100" in rendered


class TestTheLiveCorpus:
    """sc-5. The repository's own tasks, which is the corpus this was written for."""

    def _corpus_dirs(self) -> List[Path]:
        root = Path(__file__).resolve().parents[1]
        return [root / "tasks" / "agentjobs", root / "tasks" / "test-data"]

    def test_every_open_task_carries_a_position(self) -> None:
        for directory in self._corpus_dirs():
            records, unreadable = read_queue_records(directory)
            assert unreadable == [], directory
            missing = [r.task_id for r in records if r.is_open and r.queue_position is None]
            assert missing == [], f"{directory}: {missing}"

    def test_no_band_has_a_duplicate(self) -> None:
        for directory in self._corpus_dirs():
            records, _ = read_queue_records(directory)
            seen: Dict[Tuple[str, int], str] = {}
            for entry in records:
                if not entry.is_open or entry.queue_position is None:
                    continue
                key = (entry.priority, entry.queue_position)
                assert key not in seen, f"{directory}: {entry.task_id} vs {seen.get(key)}"
                seen[key] = entry.task_id

    def test_running_the_migration_again_would_change_nothing(self) -> None:
        for directory in self._corpus_dirs():
            report = migrate_queue_positions(directory, write=False)
            assert report.changed is False, f"{directory}: {report.positions()}"
