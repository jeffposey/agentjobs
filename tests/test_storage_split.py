"""A project's records live in its own database file, and how one gets moved into one.

Task-400. Two questions are pinned here and they fail differently:

*   **Resolution.** Which file does a project's rows come out of? Get this wrong and a
    project reads a database that does not hold its records, which looks like an empty
    backlog rather than like an error.
*   **The move.** ``storage split`` copies a project out of a shared database and then
    deletes the rows it copied. Every assertion about it is really an assertion about
    the order of those two things, because the second is not reversible without the
    backup the first step takes.

The assertions open the files with plain ``sqlite3`` rather than through a store. A
store asked for one project's tasks filters by ``project_id`` and would answer correctly
from a file holding all five projects, which is exactly the state these tests exist to
tell apart.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Tuple

import pytest
import yaml
from typer.testing import CliRunner

from agentjobs import cli as cli_module
from agentjobs.attachments import AttachmentPayload
from agentjobs.cli import app
from agentjobs.cutover import cut_over
from agentjobs.cutover import status as cutover_status
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
from agentjobs.projects import Project, ProjectRegistry
from agentjobs.taskfiles import TaskFileCorpus
from agentjobs.storage_config import (
    STORAGE_FILENAME,
    default_project_database,
    load_storage_settings,
    record_cutover,
)
from agentjobs.storage_split import SplitError, project_tables, split_project
from agentjobs.store_factory import close_databases

runner = CliRunner()

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

OTHER_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x02\x00\x00\x00\x02"
    b"\x08\x06\x00\x00\x00\x72\xb6\x0d\x24\x00\x00\x00\x0bIDATx\x9cc\xfc"
    b"\xff\x1f\x00\x03\x03\x02\x00\xef\xc4\x8e\x1b\x00\x00\x00\x00IEND\xaeB`\x82"
)


def a_task(task_id: str, *, position: int) -> Task:
    """One valid open record with a log entry to reconstruct history from."""
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


def build_project(root: Path, name: str, task_ids: List[str]) -> Project:
    """A project directory holding these task files, ready to be imported.

    Files, because every case here starts before the import: the module is about where
    the rows land and what moves them between files afterwards.
    """
    (root / ".agentjobs").mkdir(parents=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump({"project_name": name, "tasks_directory": "tasks"}), encoding="utf-8"
    )
    storage = TaskFileCorpus(root / "tasks")
    for index, task_id in enumerate(task_ids, start=1):
        storage.save_task(a_task(task_id, position=index * 100))
    return Project(id=name, name=name.title(), root=root)


def rows_by_project(database: Path) -> Dict[str, List[str]]:
    """Which tasks each project has in this file, read with no store in the way."""
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        found: Dict[str, List[str]] = {}
        for project_id, task_id in connection.execute(
            "SELECT project_id, task_id FROM task ORDER BY project_id, task_id"
        ):
            found.setdefault(str(project_id), []).append(str(task_id))
        return found
    finally:
        connection.close()


def counted(database: Path, sql: str, parameters: Tuple[object, ...] = ()) -> int:
    """One integer out of a database, read-only."""
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return int(connection.execute(sql, parameters).fetchone()[0])
    finally:
        connection.close()


@pytest.fixture(autouse=True)
def nothing_is_listening(monkeypatch: pytest.MonkeyPatch) -> None:
    """No server on any port, without shelling out to netstat for every test."""
    monkeypatch.setattr(cli_module, "_find_process_by_port", lambda port: None)


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A machine-level AgentJobs home nothing else has written to."""
    machine = tmp_path / "home"
    machine.mkdir()
    monkeypatch.setenv("AGENTJOBS_HOME", str(machine))
    monkeypatch.delenv("AGENTJOBS_DATABASE", raising=False)
    close_databases()
    yield machine
    close_databases()


@pytest.fixture()
def shared(home: Path, tmp_path: Path) -> Tuple[Project, Project]:
    """Two registered projects deliberately cut over into one shared database.

    The state a machine migrated before task-400 is in, and the one ``storage split``
    exists to get out of.
    """
    first = build_project(tmp_path / "alpha", "alpha", ["task-001", "task-002"])
    second = build_project(tmp_path / "beta", "beta", ["task-010"])
    registry = ProjectRegistry(home)
    registry.add(first.root, project_id="alpha")
    registry.add(second.root, project_id="beta")
    cut_over(first, backfill_git=False, shared_database=True)
    cut_over(second, backfill_git=False, shared_database=True)
    return first, second


class TestResolvingAProjectsDatabase:
    """Which file a project's rows come out of."""

    def test_an_entry_may_name_a_database(self, home: Path, tmp_path: Path) -> None:
        elsewhere = tmp_path / "volume" / "alpha.db"
        (home / STORAGE_FILENAME).write_text(
            yaml.safe_dump(
                {"projects": {"alpha": {"backend": "sqlite", "database": str(elsewhere)}}}
            ),
            encoding="utf-8",
        )
        assert load_storage_settings().database_for("alpha") == elsewhere.resolve()

    def test_a_file_with_only_the_top_level_key_answers_for_every_entry(
        self, home: Path, tmp_path: Path
    ) -> None:
        # The compatibility promise: this change adds a field, it does not require one.
        machine = tmp_path / "machine.db"
        (home / STORAGE_FILENAME).write_text(
            yaml.safe_dump(
                {
                    "database": str(machine),
                    "projects": {
                        "alpha": {"backend": "sqlite"},
                        "beta": {"backend": "sqlite"},
                    },
                }
            ),
            encoding="utf-8",
        )
        settings = load_storage_settings()
        assert settings.database_for("alpha") == machine.resolve()
        assert settings.database_for("beta") == machine.resolve()
        # A project with *no entry* is a different question and gets a different answer
        # (task-402): an entry naming no database is an operator asking to share the
        # machine's file, while no entry at all says nothing, and the honest default for
        # a project nobody has recorded is a file of its own.
        assert settings.database_for("never-registered") == default_project_database(
            "never-registered", home
        )

    def test_the_environment_still_overrides_every_project(
        self, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Decided rather than inherited: the variable stays machine-wide, so an operator
        # redirecting a machine cannot have one project quietly keep its own file.
        (home / STORAGE_FILENAME).write_text(
            yaml.safe_dump(
                {
                    "projects": {
                        "alpha": {"backend": "sqlite", "database": str(tmp_path / "a.db")},
                        "beta": {"backend": "sqlite", "database": str(tmp_path / "b.db")},
                    }
                }
            ),
            encoding="utf-8",
        )
        everything = tmp_path / "one.db"
        monkeypatch.setenv("AGENTJOBS_DATABASE", str(everything))
        settings = load_storage_settings()
        assert settings.database_for("alpha") == everything.resolve()
        assert settings.database_for("beta") == everything.resolve()

    def test_a_database_that_is_not_a_string_is_refused_by_name(self, home: Path) -> None:
        (home / STORAGE_FILENAME).write_text(
            "projects:\n  alpha:\n    backend: sqlite\n    database: 12\n", encoding="utf-8"
        )
        with pytest.raises(Exception, match="'database' for 'alpha'"):
            load_storage_settings()

    def test_distinct_files_are_listed_once_each(self, home: Path, tmp_path: Path) -> None:
        (home / STORAGE_FILENAME).write_text(
            yaml.safe_dump(
                {
                    "database": str(tmp_path / "machine.db"),
                    "projects": {
                        "alpha": {"backend": "sqlite", "database": str(tmp_path / "a.db")},
                        "beta": {"backend": "sqlite"},
                        "gamma": {"backend": "sqlite"},
                    },
                }
            ),
            encoding="utf-8",
        )
        settings = load_storage_settings()
        assert settings.databases(["alpha", "beta", "gamma"]) == [
            (tmp_path / "a.db").resolve(),
            (tmp_path / "machine.db").resolve(),
        ]


class TestACutoverGivesAProjectItsOwnFile:
    """What a new project gets, and what an operator has to ask for."""

    def test_two_projects_hold_their_records_in_different_files(
        self, home: Path, tmp_path: Path
    ) -> None:
        first = build_project(tmp_path / "alpha", "alpha", ["task-001", "task-002"])
        second = build_project(tmp_path / "beta", "beta", ["task-010"])
        cut_over(first, backfill_git=False)
        cut_over(second, backfill_git=False)

        settings = load_storage_settings()
        alpha_db = settings.database_for("alpha")
        beta_db = settings.database_for("beta")
        assert alpha_db != beta_db
        # Opened directly: a store would have filtered by project_id and answered
        # correctly from one shared file, which is the state this asserts against.
        assert rows_by_project(alpha_db) == {"alpha": ["task-001", "task-002"]}
        assert rows_by_project(beta_db) == {"beta": ["task-010"]}

    def test_the_default_is_named_for_the_project_beside_the_registry(
        self, home: Path, tmp_path: Path
    ) -> None:
        project = build_project(tmp_path / "alpha", "alpha", ["task-001"])
        cut_over(project, backfill_git=False)
        assert load_storage_settings().database_for("alpha") == home / "databases" / "alpha.db"

    def test_the_import_lands_in_the_file_the_configuration_then_records(
        self, home: Path, tmp_path: Path
    ) -> None:
        # Importing into one file and recording another is the one ordering that would
        # leave a project pointed at an empty database.
        project = build_project(tmp_path / "alpha", "alpha", ["task-001"])
        result = cut_over(project, backfill_git=False)
        assert result.database == load_storage_settings().database_for("alpha")
        assert rows_by_project(result.database) == {"alpha": ["task-001"]}

    def test_a_named_file_is_used_and_recorded(self, home: Path, tmp_path: Path) -> None:
        project = build_project(tmp_path / "alpha", "alpha", ["task-001"])
        elsewhere = tmp_path / "volume" / "alpha.db"
        cut_over(project, backfill_git=False, database=elsewhere)
        assert load_storage_settings().database_for("alpha") == elsewhere.resolve()
        assert rows_by_project(elsewhere) == {"alpha": ["task-001"]}

    def test_sharing_is_something_an_operator_asks_for(self, home: Path, tmp_path: Path) -> None:
        first = build_project(tmp_path / "alpha", "alpha", ["task-001"])
        second = build_project(tmp_path / "beta", "beta", ["task-010"])
        cut_over(first, backfill_git=False, shared_database=True)
        cut_over(second, backfill_git=False, shared_database=True)
        settings = load_storage_settings()
        assert settings.database_for("alpha") == settings.database_for("beta") == settings.database
        assert rows_by_project(settings.database) == {
            "alpha": ["task-001"],
            "beta": ["task-010"],
        }
        # And nothing was written into the entry, so the machine's key is still what
        # answers for these projects.
        written = yaml.safe_load((home / STORAGE_FILENAME).read_text(encoding="utf-8"))
        assert "database" not in written["projects"]["alpha"]


class TestWhichTablesMove:
    """The copy is decided by the schema, not by a list somebody has to maintain."""

    def test_every_project_scoped_table_is_discovered(self, home: Path, tmp_path: Path) -> None:
        project = build_project(tmp_path / "alpha", "alpha", ["task-001"])
        cut_over(project, backfill_git=False)
        connection = sqlite3.connect(load_storage_settings().database_for("alpha"))
        try:
            tables = project_tables(connection)
        finally:
            connection.close()

        assert tables[:2] == ["project", "task"]
        assert {"task_event", "task_run", "operation", "log_entry", "task_fts"} <= set(tables)
        # The fts5 shadow tables have no project_id, so discovery excludes them without
        # a skip list -- writing into one directly would corrupt the index.
        assert not any(name.startswith("task_fts_") for name in tables)


class TestTheMove:
    """``split_project``: copy, verify, record, then remove."""

    def test_the_project_ends_up_alone_in_a_file_of_its_own(
        self, shared: Tuple[Project, Project]
    ) -> None:
        first, second = shared
        report = split_project(first)
        assert report.ok, report.render()

        settings = load_storage_settings()
        assert rows_by_project(report.destination) == {"alpha": ["task-001", "task-002"]}
        assert settings.database_for("alpha") == report.destination
        assert settings.database_for("beta") == report.source

    def test_the_rows_leave_the_shared_file_and_the_other_project_is_untouched(
        self, shared: Tuple[Project, Project]
    ) -> None:
        first, _ = shared
        report = split_project(first)
        assert rows_by_project(report.source) == {"beta": ["task-010"]}
        assert (
            counted(report.source, "SELECT COUNT(*) FROM project WHERE project_id = 'alpha'") == 0
        )
        assert (
            counted(report.source, "SELECT COUNT(*) FROM task_event WHERE project_id = 'alpha'")
            == 0
        )

    def test_the_history_moves_with_the_records(self, shared: Tuple[Project, Project]) -> None:
        # A task document compares equal while its history does not, which is why the
        # verification counts rows per table as well as comparing documents.
        first, _ = shared
        events_before = counted(
            load_storage_settings().database_for("alpha"),
            "SELECT COUNT(*) FROM task_event WHERE project_id = ?",
            ("alpha",),
        )
        entries_before = counted(
            load_storage_settings().database_for("alpha"),
            "SELECT COUNT(*) FROM log_entry WHERE project_id = ?",
            ("alpha",),
        )
        assert events_before > 0 and entries_before > 0
        report = split_project(first)
        assert (
            counted(
                report.destination,
                "SELECT COUNT(*) FROM task_event WHERE project_id = ?",
                ("alpha",),
            )
            == events_before
        )
        assert (
            counted(
                report.destination,
                "SELECT COUNT(*) FROM log_entry WHERE project_id = ?",
                ("alpha",),
            )
            == entries_before
        )

    def test_search_still_works_in_the_new_file(self, shared: Tuple[Project, Project]) -> None:
        # The fts5 rows are copied like any other, and a copied index that does not
        # answer a MATCH would be a silently broken search rather than an error.
        first, _ = shared
        report = split_project(first)
        connection = sqlite3.connect(f"file:{report.destination}?mode=ro", uri=True)
        try:
            found = [
                row[0]
                for row in connection.execute(
                    "SELECT task_id FROM task_fts WHERE task_fts MATCH 'Summary'"
                )
            ]
        finally:
            connection.close()
        assert sorted(found) == ["task-001", "task-002"]
        # And it stops answering for alpha in the file it left. An index row outliving
        # its task would make a search in the shared file return ids that are not there.
        assert (
            counted(report.source, "SELECT COUNT(*) FROM task_fts WHERE project_id = 'alpha'") == 0
        )

    def test_the_source_is_backed_up_before_anything_is_removed(
        self, shared: Tuple[Project, Project]
    ) -> None:
        first, _ = shared
        report = split_project(first)
        assert report.backup is not None and report.backup.exists()
        # The snapshot is of the *shared* file as it was, so the rows the split removed
        # are still recoverable from it.
        assert rows_by_project(report.backup) == {
            "alpha": ["task-001", "task-002"],
            "beta": ["task-010"],
        }

    def test_a_failed_verification_removes_nothing_and_records_nothing(
        self, shared: Tuple[Project, Project], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentjobs import storage_split as module

        def unverified(source, destination, *, counts):
            report = module.SplitVerification(counts=dict(counts))
            report.missing = ["task-999"]
            return report

        first, _ = shared
        monkeypatch.setattr(module, "verify_split", unverified)
        report = split_project(first)

        assert not report.ok
        assert not report.recorded
        assert report.removed == {}
        # Still served from the shared file, which still holds every row.
        settings = load_storage_settings()
        assert settings.database_for("alpha") == report.source
        assert rows_by_project(report.source) == {
            "alpha": ["task-001", "task-002"],
            "beta": ["task-010"],
        }


class TestAttachments:
    """Blobs are content addressed, so they are not simply project rows."""

    @pytest.fixture()
    def with_pictures(self, home: Path, tmp_path: Path) -> Tuple[Project, Project]:
        """Two shared projects, one blob in both and one in only the first."""
        first = build_project(tmp_path / "alpha", "alpha", ["task-001"])
        second = build_project(tmp_path / "beta", "beta", ["task-010"])
        ProjectRegistry(home).add(first.root, project_id="alpha")
        ProjectRegistry(home).add(second.root, project_id="beta")

        for project, task_id, payloads in (
            (first, "task-001", [PNG, OTHER_PNG]),
            (second, "task-010", [PNG]),
        ):
            storage = TaskFileCorpus(project.root / "tasks", create=False)
            task = storage.load_task(task_id)
            assert task is not None
            attachments = [
                storage.attachments.write(task_id, AttachmentPayload(data=data, label="A"))
                for data in payloads
            ]
            task.log.append(
                LogEntry(
                    id=2,
                    ts=NOW,
                    actor="claude",
                    type=LogEntryType.NOTE,
                    body="With a picture.",
                    attachments=attachments,
                )
            )
            storage.save_task(task)

        cut_over(first, backfill_git=False, shared_database=True)
        cut_over(second, backfill_git=False, shared_database=True)
        return first, second

    def test_the_blobs_this_project_references_come_with_it(
        self, with_pictures: Tuple[Project, Project]
    ) -> None:
        first, _ = with_pictures
        report = split_project(first)
        assert report.ok, report.render()
        assert counted(report.destination, "SELECT COUNT(*) FROM blob") == 2

    def test_a_blob_the_other_project_still_references_stays_behind(
        self, with_pictures: Tuple[Project, Project]
    ) -> None:
        # The failure this pins is deleting by project rather than by refcount, which
        # would leave beta's attachment pointing at bytes that no longer exist.
        first, _ = with_pictures
        report = split_project(first)
        assert counted(report.source, "SELECT COUNT(*) FROM blob") == 1
        assert (
            counted(
                report.source,
                "SELECT COUNT(*) FROM attachment a JOIN blob b ON b.sha256 = a.sha256",
            )
            == 1
        )


class TestRefusals:
    """Every precondition it can check, it checks."""

    def test_a_project_with_no_database_yet_is_told_so(self, home: Path, tmp_path: Path) -> None:
        # It used to be told to run the cutover, because a project with no database was
        # one still on files. There is no such state now (task-402): the refusal names
        # the file it expected and says nothing was changed.
        project = build_project(tmp_path / "alpha", "alpha", ["task-001"])
        with pytest.raises(SplitError, match="does not exist"):
            split_project(project)

    def test_splitting_a_project_that_is_already_alone_is_refused(
        self, home: Path, tmp_path: Path
    ) -> None:
        project = build_project(tmp_path / "alpha", "alpha", ["task-001"])
        cut_over(project, backfill_git=False)
        with pytest.raises(SplitError, match="already served from"):
            split_project(project)

    def test_a_destination_that_exists_is_refused_rather_than_written_into(
        self, shared: Tuple[Project, Project], tmp_path: Path
    ) -> None:
        first, _ = shared
        occupied = tmp_path / "occupied.db"
        occupied.write_bytes(b"not a database")
        with pytest.raises(SplitError, match="already exists"):
            split_project(first, destination=occupied)
        assert occupied.read_bytes() == b"not a database"

    def test_a_configured_file_that_is_gone_is_named(self, home: Path, tmp_path: Path) -> None:
        project = build_project(tmp_path / "alpha", "alpha", ["task-001"])
        record_cutover(
            "alpha", project.root / "tasks", home=home, database=tmp_path / "vanished.db"
        )
        with pytest.raises(SplitError, match="does not exist"):
            split_project(project)


class TestStatusSaysWhereTheRecordsAre:
    """``storage status`` after the answer stopped being one path per machine."""

    def test_each_project_reports_its_own_file(self, shared: Tuple[Project, Project]) -> None:
        first, second = shared
        split_project(first)
        settings = load_storage_settings()
        lines = {
            line.project_id: line for line in cutover_status([first, second], settings=settings)
        }
        assert lines["alpha"].database == settings.database_for("alpha")
        assert lines["beta"].database == settings.database_for("beta")
        assert lines["alpha"].database != lines["beta"].database
        assert lines["alpha"].rows == 2
        assert lines["beta"].rows == 1

    def test_projects_sharing_a_file_are_named_to_each_other(
        self, shared: Tuple[Project, Project]
    ) -> None:
        first, second = shared
        lines = {line.project_id: line for line in cutover_status([first, second])}
        assert lines["alpha"].shared_with == ["beta"]
        assert lines["beta"].shared_with == ["alpha"]


class TestTheCommand:
    """What an operator types."""

    def test_status_prints_each_projects_database(self, shared: Tuple[Project, Project]) -> None:
        result = runner.invoke(app, ["storage", "status"])
        assert result.exit_code == 0, result.output
        assert str(load_storage_settings().database_for("alpha")) in result.output
        assert "shares that file with beta" in result.output

    def test_split_moves_the_project_and_says_where_it_went(
        self, shared: Tuple[Project, Project]
    ) -> None:
        result = runner.invoke(app, ["storage", "split", "--project", "alpha"])
        assert result.exit_code == 0, result.output
        assert "VERIFIED" in result.output
        assert "is now served from" in result.output

        settings = load_storage_settings()
        assert rows_by_project(settings.database_for("alpha")) == {
            "alpha": ["task-001", "task-002"]
        }
        assert rows_by_project(settings.database_for("beta")) == {"beta": ["task-010"]}

    def test_split_refuses_while_a_server_is_holding_the_store(
        self, shared: Tuple[Project, Project], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli_module, "_find_process_by_port", lambda port: 4321)
        result = runner.invoke(app, ["storage", "split", "--project", "alpha"])
        assert result.exit_code == 1
        assert "agentjobs stop" in result.output
        assert load_storage_settings().database_for("alpha") == load_storage_settings().database

    def test_split_exits_non_zero_and_explains_a_refusal(
        self, shared: Tuple[Project, Project]
    ) -> None:
        runner.invoke(app, ["storage", "split", "--project", "alpha"])
        again = runner.invoke(app, ["storage", "split", "--project", "alpha"])
        assert again.exit_code == 1
        assert "already served from" in again.output

    def test_backup_snapshots_every_database_on_the_machine(
        self, shared: Tuple[Project, Project]
    ) -> None:
        runner.invoke(app, ["storage", "split", "--project", "alpha"])
        result = runner.invoke(app, ["storage", "backup"])
        assert result.exit_code == 0, result.output
        settings = load_storage_settings()
        assert str(settings.database_for("alpha")) in result.output
        assert str(settings.database_for("beta")) in result.output
        assert result.output.count("VERIFIED") == 2

    def test_into_refuses_when_it_would_have_to_mean_two_files(
        self, shared: Tuple[Project, Project], tmp_path: Path
    ) -> None:
        runner.invoke(app, ["storage", "split", "--project", "alpha"])
        result = runner.invoke(app, ["storage", "backup", "--into", str(tmp_path / "one.db")])
        assert result.exit_code == 1
        assert "--project" in result.output
        assert not (tmp_path / "one.db").exists()

    def test_a_named_project_narrows_the_backup_to_its_file(
        self, shared: Tuple[Project, Project], tmp_path: Path
    ) -> None:
        runner.invoke(app, ["storage", "split", "--project", "alpha"])
        target = tmp_path / "alpha-snapshot.db"
        result = runner.invoke(
            app, ["storage", "backup", "--project", "alpha", "--into", str(target)]
        )
        assert result.exit_code == 0, result.output
        assert rows_by_project(target) == {"alpha": ["task-001", "task-002"]}

    def test_restore_refuses_to_guess_which_file_a_snapshot_replaces(
        self, shared: Tuple[Project, Project], tmp_path: Path
    ) -> None:
        # A snapshot is a whole file, so guessing here would replace one project's
        # records with another's.
        runner.invoke(app, ["storage", "split", "--project", "alpha"])
        target = tmp_path / "alpha-snapshot.db"
        runner.invoke(app, ["storage", "backup", "--project", "alpha", "--into", str(target)])

        result = runner.invoke(app, ["storage", "restore", str(target)])
        assert result.exit_code == 1
        assert "--project" in result.output

        named = runner.invoke(app, ["storage", "restore", str(target), "--project", "alpha"])
        assert named.exit_code == 0, named.output
        assert "restored" in named.output

    def test_cutover_refuses_two_contradictory_locations(self, home: Path, tmp_path: Path) -> None:
        build_project(tmp_path / "alpha", "alpha", ["task-001"])
        ProjectRegistry(home).add(tmp_path / "alpha", project_id="alpha")
        result = runner.invoke(
            app,
            [
                "storage",
                "cutover",
                "--project",
                "alpha",
                "--no-backfill-git",
                "--database",
                str(tmp_path / "a.db"),
                "--shared-database",
            ],
        )
        assert result.exit_code == 1
        assert "Pick one" in result.output
