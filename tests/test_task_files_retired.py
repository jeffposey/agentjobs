"""What stops being true once a project's records are rows rather than files.

Two workarounds existed only because task state was files in the repository being
worked, and both are retired here rather than left to be paid for:

*   the dispatcher committing the task record it wrote, because nobody else would
    (task-203);
*   the clean-tree check excluding the tasks directory, because dispatch dirtied it
    itself (task-182).

The exclusion is the more interesting of the two. It was a real loss of coverage --
a genuine change under ``tasks/`` stopped being seen -- accepted because the
alternative was refusing every dispatch. A project served from the database gets that
coverage back, and this is the test that says so.

**There is no longer a contrasting arm.** Each case here used to assert the old
behaviour on a project still served from its files and the new one after a cutover, so
that the second read as a change rather than as a tautology. The backend is gone
(task-402), so what is left is the standing assertion that neither workaround has crept
back -- and the contrast, where it can still be drawn, is in
``tests/test_dispatch_on_sqlite.py``, which refuses a dispatch on a dirty ``tasks/``.
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
from agentjobs.taskfiles import TaskFileCorpus
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
    """A project with a corpus of files, on a machine with its own AgentJobs home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AGENTJOBS_HOME", str(home))
    monkeypatch.delenv("AGENTJOBS_DATABASE", raising=False)
    root = tmp_path / "demo"
    (root / ".agentjobs").mkdir(parents=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump({"project_name": "Demo", "tasks_directory": "tasks"}), encoding="utf-8"
    )
    TaskFileCorpus(root / "tasks").save_task(a_task("task-001", 100))
    return Project(id="demo", name="Demo", root=root)


def migrated(project: Project) -> TaskManager:
    """Import the project's files and return a manager on the database."""
    from agentjobs.cutover import cut_over

    result = cut_over(project, backfill_git=False)
    assert result.ok, result.verified.render()
    with server_process():
        return TaskManager(open_store(project))


class TestTheDispatcherStopsCommitting:
    """A write that lands in a row leaves no working tree dirty."""

    def test_there_is_nothing_to_commit(self, project: Project) -> None:
        manager = migrated(project)
        outcome = commit_task_record(manager, "task-001", subject="a subject")
        assert outcome.committed is False
        assert "not a file in this checkout" in outcome.detail
        assert outcome.path is None

    def test_it_is_answered_as_a_fact_rather_than_caught_as_a_failure(
        self, project: Project
    ) -> None:
        # Answered, not raised. A caller that discovered this by catching an exception
        # would be branching on a failure rather than on a fact, and would swallow a
        # real error the same way.
        outcome = commit_task_record(migrated(project), "task-001")
        assert outcome.committed is False
        assert outcome.path is None


class TestTheCleanTreeExceptionIsRetired:
    """task-182's workaround has nothing left to cover."""

    def test_nothing_is_excluded(self, project: Project) -> None:
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
        database = load_storage_settings().database_for(project.id)
        assert project.root not in database.parents
        assert database.exists()
