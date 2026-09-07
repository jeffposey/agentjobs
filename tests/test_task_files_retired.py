"""What stops being true once a project's records are rows rather than files.

Two workarounds existed only because task state was files in the repository being
worked, and both are retired here rather than left to be paid for:

*   the dispatcher committing the task record it wrote, because nobody else would
    (task-203);
*   the clean-tree check excluding the tasks directory, because dispatch dirtied it
    itself (task-182).

The exclusion is the more interesting of the two. It was a real loss of coverage --
a genuine change under ``tasks/`` stopped being seen -- accepted because the
alternative was refusing every dispatch. A migrated project gets that coverage back,
and this is the test that says so.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agentjobs.dispatch.record_commit import commit_task_record, task_file_exclusions
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import (
    Assignment,
    Ball,
    BallReason,
    Lifecycle,
    LogEntry,
    LogEntryType,
    Priority,
    Spec,
    Task,
)
from agentjobs.projects import Project
from agentjobs.storage import TaskStorage
from agentjobs.storage_config import load_storage_settings
from agentjobs.store_factory import open_store, server_process

NOW = "2026-09-01T12:00:00Z"


def a_task(task_id: str, position: int) -> Task:
    return Task(
        id=task_id,
        title=f"Task {task_id}",
        created=NOW,
        updated=NOW,
        lifecycle=Lifecycle.READY,
        ball=Ball.AGENT,
        ball_reason=BallReason.AVAILABLE,
        priority=Priority.HIGH,
        queue_position=position,
        category="infrastructure",
        assignment=Assignment(),
        spec=Spec(summary="Summary", description="A description."),
        log=[
            LogEntry(
                id=1,
                ts=NOW,
                actor="claude",
                type=LogEntryType.TRANSITION,
                body="Created ready by claude.",
                data={"lifecycle": "ready"},
            )
        ],
    )


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Project:
    """A project on files, on a machine with its own AgentJobs home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AGENTJOBS_HOME", str(home))
    monkeypatch.delenv("AGENTJOBS_DATABASE", raising=False)
    root = tmp_path / "demo"
    (root / ".agentjobs").mkdir(parents=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump({"project_name": "Demo", "tasks_directory": "tasks"}), encoding="utf-8"
    )
    storage = TaskStorage(root / "tasks")
    storage.save_task(a_task("task-001", 100))
    return Project(id="demo", name="Demo", root=root)


def migrated(project: Project) -> TaskManager:
    """Cut the project over and return a manager on the database."""
    from agentjobs.cutover import cut_over

    result = cut_over(project, backfill_git=False)
    assert result.ok, result.verified.render()
    with server_process():
        return TaskManager(open_store(project))


class TestTheDispatcherStopsCommitting:
    """A write that lands in a row leaves no working tree dirty."""

    def test_a_file_backed_project_still_has_a_path_to_commit(self, project: Project) -> None:
        manager = TaskManager(open_store(project))
        # Not a git repository here, so the attempt gets that far and stops there --
        # which is enough to show it looked for a file rather than declining outright.
        outcome = commit_task_record(manager, "task-001", subject="a subject")
        assert "not inside a git repository" in outcome.detail

    def test_a_migrated_project_has_nothing_to_commit(self, project: Project) -> None:
        manager = migrated(project)
        outcome = commit_task_record(manager, "task-001", subject="a subject")
        assert outcome.committed is False
        assert "not a file in this checkout" in outcome.detail
        assert outcome.path is None

    def test_it_is_answered_as_a_fact_rather_than_caught_as_a_failure(
        self, project: Project
    ) -> None:
        # The store says whether a record is a file. A caller that discovered this by
        # catching an exception would be branching on a failure rather than on a fact,
        # and would swallow a real error the same way.
        assert TaskStorage(project.root / "tasks").supports_task_files is True
        assert migrated(project).storage.supports_task_files is False


class TestTheCleanTreeExceptionIsRetired:
    """task-182's workaround has nothing left to cover."""

    def test_a_file_backed_project_still_excludes_its_tasks_directory(
        self, project: Project
    ) -> None:
        manager = TaskManager(open_store(project))
        assert task_file_exclusions(manager) == [project.tasks_dir()]

    def test_a_migrated_project_excludes_nothing(self, project: Project) -> None:
        # The coverage task-182 had to give up: a real change under `tasks/` was
        # invisible to the dispatch gate. Nothing writes there any more, so nothing
        # needs excusing and the check sees the whole tree again.
        assert task_file_exclusions(migrated(project)) == []


class TestNothingDirtiesTheCheckout:
    """The property sc-2 asks for, asserted on the filesystem rather than inferred."""

    def test_a_verb_after_the_cutover_writes_no_file_into_the_project(
        self, project: Project
    ) -> None:
        manager = migrated(project)
        before = sorted(path.name for path in (project.root / "tasks").glob("*.yaml"))
        stamps = {
            path.name: path.stat().st_mtime for path in (project.root / "tasks").glob("*.yaml")
        }

        manager.add_log_entry(
            "task-001", actor="claude", type=LogEntryType.PROGRESS, body="Worked on it."
        )
        manager.claim_task("task-001", agent="claude")

        after = sorted(path.name for path in (project.root / "tasks").glob("*.yaml"))
        assert after == before
        assert {
            path.name: path.stat().st_mtime for path in (project.root / "tasks").glob("*.yaml")
        } == stamps
        # And the write did land -- otherwise this test would pass on a manager that
        # does nothing at all.
        written = manager.get_task("task-001")
        assert written is not None and written.assignment.owner == "claude"

    def test_the_database_is_outside_every_checkout(self, project: Project) -> None:
        migrated(project)
        database = load_storage_settings().database
        assert project.root not in database.parents
        assert database.exists()
