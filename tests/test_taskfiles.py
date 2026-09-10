"""The task-file format, and the store surface the old file backend's tests pinned.

This was ``test_storage.py`` and it tested a backend. Half of what it asserted is about
a *file* -- the schema stamp, what a loader does with YAML it cannot parse, an
unmigrated v1 document -- and that is still worth having: those files are what an import
reads and what an export writes, so the format is a contract with everyone arriving with
a corpus or leaving with one. Those cases moved to :class:`TaskFileCorpus` unchanged.

The other half -- search, deletion, a cheap revision signal -- is about a *store*, and
went with the ids rather than with the files. Those cases are here too, over the store
that exists, because nothing else in the suite pinned them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentjobs.models_v2 import Ball, BallReason, Lifecycle, Priority, Spec, Task
from agentjobs.taskfiles import TaskFileCorpus, TaskLoadError
from support import task_store


def _build_task(task_id: str, title: str = "Sample") -> Task:
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return Task(
        id=task_id,
        title=title,
        created=now,
        updated=now,
        lifecycle=Lifecycle.READY,
        ball=Ball.AGENT,
        ball_reason=BallReason.AVAILABLE,
        priority=Priority.MEDIUM,
        queue_position=int(task_id.split("-")[1]) * 100,
        category="general",
        spec=Spec(summary="A sample task.", description="A sample task."),
    )


# ---------------------------------------------------------------------------
# The file format: what an import reads and an export writes
# ---------------------------------------------------------------------------


class TestTheFileFormat:
    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        corpus = TaskFileCorpus(tmp_path)
        task = _build_task("task-001")
        stored = corpus.save_task(task)

        assert stored.updated >= task.created
        reloaded = corpus.load_task("task-001.yaml")
        assert reloaded is not None
        assert reloaded.id == "task-001"
        assert reloaded.title == task.title

    def test_the_written_file_carries_the_schema_stamp_by_alias(self, tmp_path: Path) -> None:
        """The stamp is written as `schema:`, not the Python-side `schema_version`.

        `schema` shadows a BaseModel attribute, so the field is aliased in Python; a dump
        without by_alias writes the wrong key and produces a file the loader rejects as
        v1 -- which would make every export unreadable by the import that reads it back.
        """
        corpus = TaskFileCorpus(tmp_path)
        corpus.save_task(_build_task("task-002"))

        text = (tmp_path / "task-002.yaml").read_text(encoding="utf-8")
        assert text.startswith("schema: 2")
        assert "schema_version" not in text
        # display_status is computed for API responses and must never be stored.
        assert "display_status" not in text

    def test_a_file_that_will_not_parse_raises_instead_of_vanishing(self, tmp_path: Path) -> None:
        """task-049. A record that disappears is the worst available failure mode."""
        corpus = TaskFileCorpus(tmp_path)

        (tmp_path / "task-bad.yaml").write_text("foo: [unterminated", encoding="utf-8")
        with pytest.raises(TaskLoadError, match="invalid YAML"):
            corpus.load_task("task-bad")

        (tmp_path / "task-empty.yaml").write_text("", encoding="utf-8")
        with pytest.raises(TaskLoadError, match="empty"):
            corpus.load_task("task-empty")

        (tmp_path / "task-invalid.yaml").write_text(
            "schema: 2" + chr(10) + "id: missing-fields" + chr(10), encoding="utf-8"
        )
        with pytest.raises(TaskLoadError) as caught:
            corpus.load_task("task-invalid")
        # The point of the change: the message names the file and the fields.
        assert "task-invalid.yaml" in str(caught.value)
        assert "title" in str(caught.value)

    def test_an_unmigrated_v1_file_is_reported_by_filename(self, tmp_path: Path) -> None:
        """A file with no `schema: 2` stamp names the migrator rather than vanishing.

        This is what an operator meets when they point an import at a corpus nobody
        migrated, and it must be one named file in a report rather than a failed run.
        """
        corpus = TaskFileCorpus(tmp_path)
        (tmp_path / "task-old.yaml").write_text(
            "id: task-old"
            + chr(10)
            + "title: Old"
            + chr(10)
            + "status: ready"
            + chr(10)
            + "category: misc"
            + chr(10)
            + "description: x"
            + chr(10),
            encoding="utf-8",
        )
        with pytest.raises(TaskLoadError, match="migrate-schema") as caught:
            corpus.load_task("task-old")
        assert "task-old.yaml" in str(caught.value)

        result = corpus.load_all()
        assert result.tasks == []
        assert len(result.errors) == 1
        assert result.errors[0].task_id == "task-old"

    def test_a_missing_file_is_a_plain_none(self, tmp_path: Path) -> None:
        """Absent is not the same as broken, and conflating them is the bug."""
        assert TaskFileCorpus(tmp_path).load_task("task-does-not-exist") is None

    def test_one_unreadable_file_does_not_take_down_the_read(self, tmp_path: Path) -> None:
        corpus = TaskFileCorpus(tmp_path)
        corpus.save_task(_build_task("task-001"))
        (tmp_path / "task-bad.yaml").write_text("foo: [unterminated", encoding="utf-8")

        result = corpus.load_all()

        assert [task.id for task in result.tasks] == ["task-001"]
        assert [error.path.name for error in result.errors] == ["task-bad.yaml"]

    def test_only_yaml_is_read(self, tmp_path: Path) -> None:
        """Artifacts beside the task files never appear as tasks."""
        corpus = TaskFileCorpus(tmp_path)
        corpus.save_task(_build_task("task-001"))
        (tmp_path / "task-001.lock").write_text("1234", encoding="utf-8")
        (tmp_path / "notes.md").write_text("prose", encoding="utf-8")

        result = corpus.load_all()

        assert [task.id for task in result.tasks] == ["task-001"]
        assert result.errors == []


# ---------------------------------------------------------------------------
# The store surface these tests pinned, over the store that holds the records
# ---------------------------------------------------------------------------


class TestTheStoreSurface:
    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        storage = task_store(tmp_path)
        task = _build_task("task-001")
        storage.save_task(task)

        reloaded = storage.load_task("task-001.yaml")
        assert reloaded is not None
        assert reloaded.id == "task-001"
        assert reloaded.title == task.title

    def test_a_missing_task_is_none(self, tmp_path: Path) -> None:
        assert task_store(tmp_path).load_task("task-does-not-exist") is None

    def test_list_and_search(self, tmp_path: Path) -> None:
        storage = task_store(tmp_path)
        storage.save_task(_build_task("task-001", title="Implement feature"))
        storage.save_task(_build_task("task-002", title="Write docs"))

        assert len(storage.list_tasks()) == 2
        matches = storage.search_tasks("docs")
        assert [task.id for task in matches] == ["task-002"]

    @pytest.mark.parametrize("query", ["task-058", "058", "TASK-058"])
    def test_search_matches_the_task_id(self, tmp_path: Path, query: str) -> None:
        """The id is the handle people quote, so a bare number has to find its task."""
        storage = task_store(tmp_path)
        storage.save_task(_build_task("task-058-multi-project-gui", title="Multi project GUI"))
        storage.save_task(_build_task("task-101-unrelated", title="Something else"))

        matches = storage.search_tasks(query)
        assert [task.id for task in matches] == ["task-058-multi-project-gui"]

    def test_the_revision_signal_moves_on_a_change_and_counts_the_tasks(
        self, tmp_path: Path
    ) -> None:
        """What the React live-update poll asks, fifteen seconds apart, forever."""
        storage = task_store(tmp_path)
        storage.save_task(_build_task("task-001"))
        initial = storage.project_revision()

        storage.save_task(_build_task("task-001", title="Changed"))
        storage.save_task(_build_task("task-002"))
        changed = storage.project_revision()

        storage.delete_task("task-002")
        deleted = storage.project_revision()

        assert initial[1] == 1
        assert changed[1] == 2
        assert deleted[1] == 1
        assert len({initial[0], changed[0], deleted[0]}) == 3

    def test_delete_task(self, tmp_path: Path) -> None:
        storage = task_store(tmp_path)
        storage.save_task(_build_task("task-050"))

        assert storage.delete_task("task-050") is True
        assert storage.load_task("task-050") is None
        assert storage.delete_task("task-050") is False
