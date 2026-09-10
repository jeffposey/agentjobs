"""The operator sequence: preview, back up, import, verify, record -- and back again.

Every test here works on a directory of real task YAML written by ``SqlTaskStore``, so
what is being exercised is the path a machine actually takes rather than a fixture
shaped to suit the importer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import List

import pytest
import yaml

from agentjobs.cutover import (
    back_up,
    cut_over,
    export_project,
    import_project,
    preview,
    status,
    verify_backup,
    verify_import,
)
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import (
    Assignment,
    Ball,
    BallReason,
    Lifecycle,
    LogEntry,
    LogEntryType,
    Outcome,
    Priority,
    Spec,
    Task,
)
from agentjobs.projects import Project
from agentjobs.sqlstore import CorpusAlreadyImported, SqlTaskStore
from agentjobs.taskfiles import TaskFileCorpus
from agentjobs.storage_config import (
    default_project_database,
    load_storage_settings,
)
from agentjobs.store_factory import open_database, open_store, server_process

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def a_task(task_id: str, *, position: int, closed: bool = False) -> Task:
    """One valid record, open or closed, with a log entry to reconstruct from."""
    return Task(
        id=task_id,
        title=f"Task {task_id}",
        created=NOW,
        updated=NOW,
        lifecycle=Lifecycle.CLOSED if closed else Lifecycle.READY,
        outcome=Outcome.COMPLETED if closed else None,
        ball=None if closed else Ball.AGENT,
        ball_reason=None if closed else BallReason.AVAILABLE,
        priority=Priority.HIGH,
        queue_position=None if closed else position,
        category="infrastructure",
        assignment=Assignment(),
        spec=Spec(summary=f"Summary for {task_id}", description="A description."),
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
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A machine-level home nothing else has written to."""
    machine = tmp_path / "home"
    machine.mkdir()
    monkeypatch.setenv("AGENTJOBS_HOME", str(machine))
    monkeypatch.delenv("AGENTJOBS_DATABASE", raising=False)
    return machine


@pytest.fixture()
def project(tmp_path: Path) -> Project:
    """A project whose tasks directory holds three records, two open and one closed."""
    root = tmp_path / "demo"
    (root / ".agentjobs").mkdir(parents=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump({"project_name": "Demo", "tasks_directory": "tasks"}), encoding="utf-8"
    )
    storage = TaskFileCorpus(root / "tasks")
    storage.save_task(a_task("task-001", position=100))
    storage.save_task(a_task("task-002", position=200))
    storage.save_task(a_task("task-003", position=300, closed=True))
    return Project(id="demo", name="Demo", root=root)


def stored_tasks(project: Project) -> List[Task]:
    """Every task the database holds for this project."""
    with server_process():
        database = open_database(load_storage_settings().database_for(project.id))
        return SqlTaskStore(database, project.id).list_tasks()


class TestPreview:
    """The dry run is the real import, against a database that does not survive it."""

    def test_it_reports_what_a_real_import_would_do(self, home: Path, project: Project) -> None:
        imported, verified = preview(project)
        assert imported.imported == 3
        assert imported.reconciles
        assert verified.ok
        assert verified.rows == verified.files == 3

    def test_it_writes_nothing_that_survives_the_call(self, home: Path, project: Project) -> None:
        preview(project)
        # No database, and no configuration saying anything migrated.
        settings = load_storage_settings()
        assert not settings.database.exists()
        assert not default_project_database(project.id, settings.home).exists()
        assert settings.for_project(project.id).cutover_at is None

    def test_an_unreadable_record_is_named_rather_than_dropped(
        self, home: Path, project: Project
    ) -> None:
        (project.root / "tasks" / "task-666.yaml").write_text(
            "id: task-666\ntitle: broken\n", encoding="utf-8"
        )
        imported, verified = preview(project)
        assert any("task-666" in name for name, _ in imported.quarantined)
        assert any("task-666" in name for name, _ in verified.unreadable)
        # The three good records still arrived: one bad file does not stop the corpus.
        assert imported.imported == 3

    def test_the_verdict_refuses_to_read_as_a_clean_bill(
        self, home: Path, project: Project
    ) -> None:
        """A quarantined record is compared against nothing, so ``ok`` stays true.

        That is the correct meaning of ``ok`` -- every file that could be read arrived
        unchanged -- and it is not the question an operator is asking, which is whether
        their backlog arrived. Three real previews reported VERIFIED on 2026-09-08 while
        together planning to leave 17 open tasks behind (task-378), so the last line now
        says so rather than leaving it to the lines above it.
        """
        (project.root / "tasks" / "task-666.yaml").write_text(
            "id: task-666\ntitle: broken\n", encoding="utf-8"
        )
        _, verified = preview(project)

        assert verified.ok, "the readable files did all arrive; that is what ok means"
        rendered = verified.render()
        assert rendered.splitlines()[-1].startswith("VERIFIED -- but 1 record(s)")
        assert "queue repair" in rendered

    def test_a_corpus_with_nothing_quarantined_still_says_only_verified(
        self, home: Path, project: Project
    ) -> None:
        """The qualification is a warning, not new boilerplate on a clean run."""
        _, verified = preview(project)

        assert verified.render().splitlines()[-1] == "VERIFIED"


class TestImportAndVerify:
    """What the store holds afterwards, checked field by field rather than counted."""

    def test_every_record_arrives_unchanged(self, home: Path, project: Project) -> None:
        _, verified = import_project(project)
        assert verified.ok, verified.render()
        assert verified.missing == [] and verified.differing == []

    def test_the_records_own_timestamps_survive(self, home: Path, project: Project) -> None:
        # An import is not an update. Restamping `updated` would silently re-date the
        # whole corpus to the migration, which no count would notice -- so the stamp
        # each file carries has to be the stamp its row carries.
        on_disk = {
            task.id: task.updated for task in TaskFileCorpus(project.root / "tasks").list_tasks()
        }
        import_project(project)
        assert {task.id: task.updated for task in stored_tasks(project)} == on_disk

    def test_a_second_import_is_refused(self, home: Path, project: Project) -> None:
        import_project(project)
        with pytest.raises(CorpusAlreadyImported):
            import_project(project)

    def test_replacing_leaves_exactly_one_copy(self, home: Path, project: Project) -> None:
        first, _ = import_project(project)
        again, verified = import_project(project, replace=True)
        assert again.imported == first.imported
        assert again.events == first.events
        assert verified.ok
        assert len(stored_tasks(project)) == 3

    def test_verification_catches_a_record_that_did_not_arrive(
        self, home: Path, project: Project
    ) -> None:
        import_project(project)
        # A file appearing after the import is exactly what a missed record looks like.
        TaskFileCorpus(project.root / "tasks").save_task(a_task("task-004", position=400))
        with server_process():
            settings = load_storage_settings()
            store = SqlTaskStore(open_database(settings.database_for(project.id)), project.id)
            report = verify_import(store, project.root / "tasks")
        assert not report.ok
        assert report.missing == ["task-004"]


class TestCutover:
    """Backing up, switching, and refusing to switch."""

    def test_it_records_the_switch_only_after_verification_passes(
        self, home: Path, project: Project
    ) -> None:
        result = cut_over(project, backfill_git=False)
        assert result.ok
        assert load_storage_settings().for_project(project.id).cutover_at is not None

    def test_the_server_then_serves_the_database(self, home: Path, project: Project) -> None:
        cut_over(project, backfill_git=False)
        with server_process():
            store = open_store(project)
        assert isinstance(store, SqlTaskStore)
        assert {task.id for task in store.list_tasks()} == {"task-001", "task-002", "task-003"}

    def test_the_first_cutover_on_a_machine_has_nothing_to_back_up(
        self, home: Path, project: Project
    ) -> None:
        result = cut_over(project, backfill_git=False)
        assert result.backup is None

    def test_a_later_backup_verifies_as_restorable(self, home: Path, project: Project) -> None:
        cut_over(project, backfill_git=False)
        settings = load_storage_settings()
        snapshot_path = back_up(settings, database=settings.database_for(project.id))
        assert snapshot_path is not None and snapshot_path.exists()
        assert verify_backup(snapshot_path).ok

    def test_a_failed_verification_records_nothing(
        self, home: Path, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentjobs import cutover as module

        def unverified(store: SqlTaskStore, tasks_dir: Path) -> module.VerificationReport:
            report = module.VerificationReport()
            report.missing = ["task-999"]
            return report

        monkeypatch.setattr(module, "verify_import", unverified)
        result = module.cut_over(project, backfill_git=False)
        assert not result.ok
        assert not result.recorded
        assert load_storage_settings().for_project(project.id).cutover_at is None


class TestGettingBackOut:
    """Export is the interchange artifact, and the road out is only that.

    There is no rollback: the backend a rollback returned a project to was deleted by
    task-402. What survives is the property the rollback was built on -- an export is a
    directory this codebase can read back -- which is what makes moving a project
    between machines possible at all.
    """

    def test_an_export_is_a_directory_that_can_be_imported_again(
        self, home: Path, project: Project, tmp_path: Path
    ) -> None:
        cut_over(project, backfill_git=False)
        destination = tmp_path / "exported"
        report = export_project(project, destination)
        assert report.tasks == 3

        served = TaskFileCorpus(destination, create=False)
        assert {task.id for task in served.list_tasks()} == {
            "task-001",
            "task-002",
            "task-003",
        }

    def test_an_export_carries_what_was_written_after_the_import(
        self, home: Path, project: Project, tmp_path: Path
    ) -> None:
        cut_over(project, backfill_git=False)
        with server_process():
            manager = TaskManager(open_store(project))
            manager.add_log_entry(
                "task-001", actor="claude", type=LogEntryType.PROGRESS, body="Written after."
            )

        export_project(project, tmp_path / "exported")

        # The store's *current* state, not the snapshot taken before the import.
        # Exporting the snapshot instead would hand somebody a corpus missing a day's
        # work with nothing saying so.
        recovered = TaskFileCorpus(tmp_path / "exported", create=False).load_task("task-001")
        assert recovered is not None
        assert any(entry.body == "Written after." for entry in recovered.log)

    def test_an_export_leaves_the_database_alone(
        self, home: Path, project: Project, tmp_path: Path
    ) -> None:
        cut_over(project, backfill_git=False)
        export_project(project, tmp_path / "exported")
        assert load_storage_settings().database_for(project.id).exists()
        assert len(stored_tasks(project)) == 3

    def test_an_attachment_comes_back_as_bytes_beside_the_task(
        self, home: Path, project: Project, tmp_path: Path
    ) -> None:
        from agentjobs.attachments import AttachmentPayload

        png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
            b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        storage = TaskFileCorpus(project.root / "tasks")
        task = storage.load_task("task-001")
        assert task is not None
        attachment = storage.attachments.write("task-001", AttachmentPayload(data=png, label="A"))
        task.log.append(
            LogEntry(
                id=2,
                ts=NOW,
                actor="claude",
                type=LogEntryType.NOTE,
                body="With a picture.",
                attachments=[attachment],
            )
        )
        storage.save_task(task)

        cut_over(project, backfill_git=False)
        destination = tmp_path / "exported"
        report = export_project(project, destination)

        assert report.attachments == 1
        assert (destination / attachment.path).read_bytes() == png


class TestStatus:
    """The first question an operator asks, answered by counting rather than assuming."""

    def test_it_reports_both_counts_before_and_after_an_import(
        self, home: Path, project: Project
    ) -> None:
        before = status([project])[0]
        assert before.files == 3
        assert before.rows is None  # no database on this machine yet

        cut_over(project, backfill_git=False)
        after = status([project])[0]
        assert after.rows == 3
        # Both counts, deliberately: files still on disk after a cutover is a real and
        # informative state, and so is rows without files once retirement has run.
        assert after.files == 3
        assert after.cutover_at is not None
