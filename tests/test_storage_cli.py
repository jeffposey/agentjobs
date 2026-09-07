"""``agentjobs storage`` -- the operator's half of the cutover.

The sequence itself is tested in ``tests/test_cutover.py``. What is tested here is the
command surface an operator actually types: that a dry run reports without switching,
that the switch refuses while a server is holding the store, that a failed verification
exits non-zero, and that the messages say what to do next.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from agentjobs import cli as cli_module
from agentjobs.cli import app
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
from agentjobs.projects import ProjectRegistry
from agentjobs.storage import TaskStorage
from agentjobs.storage_config import FILES, SQLITE, load_storage_settings

runner = CliRunner()

NOW = "2026-09-01T12:00:00Z"


@pytest.fixture(autouse=True)
def nothing_is_listening(monkeypatch: pytest.MonkeyPatch) -> None:
    """No server on any port, without shelling out to netstat for every test."""
    monkeypatch.setattr(cli_module, "_find_process_by_port", lambda port: None)


@pytest.fixture()
def registered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A registered project with two task files, on a machine with no database."""
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
    for index, task_id in enumerate(("task-001", "task-002"), start=1):
        storage.save_task(
            Task(
                id=task_id,
                title=f"Task {index}",
                created=NOW,
                updated=NOW,
                lifecycle=Lifecycle.READY,
                ball=Ball.AGENT,
                ball_reason=BallReason.AVAILABLE,
                priority=Priority.HIGH,
                queue_position=index * 100,
                category="infrastructure",
                assignment=Assignment(),
                spec=Spec(summary=f"Summary {index}", description="A description."),
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
        )
    ProjectRegistry(home).add(root, project_id="demo")
    monkeypatch.chdir(root)
    return root


class TestStatus:
    """The first question an operator asks."""

    def test_it_says_files_before_anything_has_been_migrated(self, registered: Path) -> None:
        result = runner.invoke(app, ["storage", "status"])
        assert result.exit_code == 0, result.output
        assert "demo: files" in result.output
        assert "files=2" in result.output
        assert "none yet" in result.output  # no database on this machine

    def test_it_says_sqlite_afterwards_with_both_counts(self, registered: Path) -> None:
        runner.invoke(app, ["storage", "cutover", "--no-backfill-git"])
        result = runner.invoke(app, ["storage", "status"])
        assert "demo: sqlite" in result.output
        assert "rows=2" in result.output
        assert "cut over" in result.output


class TestPreview:
    """A dry run reports and switches nothing."""

    def test_it_reports_and_leaves_the_project_on_files(self, registered: Path) -> None:
        result = runner.invoke(app, ["storage", "preview"])
        assert result.exit_code == 0, result.output
        assert "imported 2 tasks" in result.output
        assert "VERIFIED" in result.output
        assert load_storage_settings().backend_for("demo") == FILES
        assert not load_storage_settings().database.exists()

    def test_an_unreadable_record_is_named(self, registered: Path) -> None:
        (registered / "tasks" / "task-666.yaml").write_text("id: task-666\n", encoding="utf-8")
        result = runner.invoke(app, ["storage", "preview"])
        assert "task-666" in result.output
        assert "quarantined" in result.output


class TestCutover:
    """Switching, and refusing to."""

    def test_it_switches_and_says_what_to_do_next(self, registered: Path) -> None:
        result = runner.invoke(app, ["storage", "cutover", "--no-backfill-git"])
        assert result.exit_code == 0, result.output
        assert "is now served from" in result.output
        assert "agentjobs serve" in result.output
        # The operator backup-enrolment checkpoint, printed at the one moment somebody
        # is certain to be reading: the database is now the only copy of the
        # reconstructed history.
        assert "backups" in result.output
        assert load_storage_settings().backend_for("demo") == SQLITE

    def test_it_refuses_while_a_server_is_holding_the_store(
        self, registered: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli_module, "_find_process_by_port", lambda port: 4321)
        result = runner.invoke(app, ["storage", "cutover"])
        assert result.exit_code == 1
        assert "agentjobs stop" in result.output
        assert load_storage_settings().backend_for("demo") == FILES

    def test_a_second_cutover_is_refused_rather_than_doubling_history(
        self, registered: Path
    ) -> None:
        runner.invoke(app, ["storage", "cutover", "--no-backfill-git"])
        again = runner.invoke(app, ["storage", "cutover", "--no-backfill-git"])
        assert again.exit_code == 1
        assert "--replace" in again.output

    def test_replace_re_imports(self, registered: Path) -> None:
        runner.invoke(app, ["storage", "cutover", "--no-backfill-git"])
        again = runner.invoke(app, ["storage", "cutover", "--no-backfill-git", "--replace"])
        assert again.exit_code == 0, again.output
        assert "VERIFIED" in again.output


class TestBackupAndRestore:
    """A snapshot that is not verified is not a backup."""

    def test_backup_writes_a_snapshot_and_verifies_it(
        self, registered: Path, tmp_path: Path
    ) -> None:
        runner.invoke(app, ["storage", "cutover", "--no-backfill-git"])
        target = tmp_path / "snapshots" / "one.db"
        result = runner.invoke(app, ["storage", "backup", "--into", str(target)])
        assert result.exit_code == 0, result.output
        assert target.exists()
        assert (tmp_path / "snapshots" / "one.db.manifest.json").exists()

    def test_backup_on_a_machine_with_no_database_says_so(self, registered: Path) -> None:
        result = runner.invoke(app, ["storage", "backup"])
        assert result.exit_code == 0
        assert "nothing to back up" in result.output

    def test_verify_reads_a_snapshot_without_touching_the_live_store(
        self, registered: Path, tmp_path: Path
    ) -> None:
        runner.invoke(app, ["storage", "cutover", "--no-backfill-git"])
        target = tmp_path / "one.db"
        runner.invoke(app, ["storage", "backup", "--into", str(target)])
        result = runner.invoke(app, ["storage", "verify", str(target)])
        assert result.exit_code == 0, result.output

    def test_restore_puts_the_snapshot_back(self, registered: Path, tmp_path: Path) -> None:
        runner.invoke(app, ["storage", "cutover", "--no-backfill-git"])
        target = tmp_path / "one.db"
        runner.invoke(app, ["storage", "backup", "--into", str(target)])

        result = runner.invoke(app, ["storage", "restore", str(target)])
        assert result.exit_code == 0, result.output
        assert "restored" in result.output


class TestGettingBackOut:
    """Export and rollback."""

    def test_export_writes_a_directory_the_file_backend_can_serve(
        self, registered: Path, tmp_path: Path
    ) -> None:
        runner.invoke(app, ["storage", "cutover", "--no-backfill-git"])
        destination = tmp_path / "exported"
        result = runner.invoke(app, ["storage", "export", str(destination)])
        assert result.exit_code == 0, result.output
        assert {task.id for task in TaskStorage(destination).list_tasks()} == {
            "task-001",
            "task-002",
        }

    def test_rollback_returns_the_project_to_its_files(self, registered: Path) -> None:
        runner.invoke(app, ["storage", "cutover", "--no-backfill-git"])
        result = runner.invoke(app, ["storage", "rollback"])
        assert result.exit_code == 0, result.output
        assert load_storage_settings().backend_for("demo") == FILES
        assert len(list((registered / "tasks").glob("*.yaml"))) == 2


class TestTheFileEraCommandsAfterACutover:
    """A command about *files* must not answer from a directory nothing writes.

    After a cutover the old directory is still in the checkout, frozen at the moment of
    the migration. ``validate`` and ``migrate-schema`` would keep working over it, keep
    passing, and keep answering about a corpus that has moved on -- which is worse than
    an error, because nothing in the output says the answer is stale.
    """

    def test_validate_refuses_and_says_where_the_records_are(self, registered: Path) -> None:
        runner.invoke(app, ["storage", "cutover", "--no-backfill-git"])
        result = runner.invoke(app, ["validate"])
        assert result.exit_code == 1
        assert "served from the AgentJobs database" in result.output
        assert "storage status" in result.output

    def test_migrate_schema_refuses_too(self, registered: Path) -> None:
        runner.invoke(app, ["storage", "cutover", "--no-backfill-git"])
        result = runner.invoke(app, ["migrate-schema"])
        assert result.exit_code == 1
        assert "no files to read" in result.output

    def test_they_still_work_before_a_cutover(self, registered: Path) -> None:
        result = runner.invoke(app, ["validate"])
        assert result.exit_code == 0, result.output

    def test_quotations_asks_the_service_rather_than_the_frozen_copy(
        self, registered: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Not a refusal: it is about prose, and the prose still exists -- so it follows
        # the records instead of the directory. With the project migrated and no server
        # running, it reports that the service did not answer. Reading the stale files
        # would have looked like success, which is the failure this pins.
        monkeypatch.setattr(cli_module, "RETRY_BACKOFF_SECONDS", (0.0,), raising=False)
        from agentjobs import client as client_module

        monkeypatch.setattr(client_module, "RETRY_BACKOFF_SECONDS", (0.0,))

        runner.invoke(app, ["storage", "cutover", "--no-backfill-git"])
        result = runner.invoke(app, ["quotations"])

        assert result.exit_code != 0
        assert isinstance(result.exception, client_module.ServiceUnavailable)
        assert "nothing written" in str(result.exception)
