"""What a fresh install gets: rows in its own database, and no tasks directory.

task-399. The subject is ``agentjobs init``, and the assertions are about what exists
on disk afterwards rather than about what the command printed -- a project that "is on
sqlite" and has no database file is a state an operator cannot act on.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from agentjobs.cli import app
from agentjobs.projects import ProjectRegistry, default_home
from agentjobs.storage_config import (
    ProjectStorage,
    load_storage_settings,
    record_cutover,
    record_new_project,
)

runner = CliRunner()

INIT = [
    "init",
    "--project-name",
    "Fresh Project",
    "--prompts-dir",
    "prompts",
    "--port",
    "8765",
    "--user",
    "jeff",
]


def _init(extra: list[str] | None = None) -> object:
    return runner.invoke(app, INIT + (extra or []), catch_exceptions=False)


class TestANewProjectIsOnTheDatabase:
    def test_no_tasks_directory_is_created(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)

        result = _init()

        assert result.exit_code == 0, result.output
        assert not (tmp_path / "tasks").exists()

    def test_the_project_is_recorded_on_sqlite(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)

        _init()

        entry = load_storage_settings().for_project("fresh-project")
        assert entry.backend == "sqlite"
        assert entry.on_sqlite

    def test_the_database_file_exists_afterwards(self, tmp_path: Path, monkeypatch) -> None:
        """Created at init, not left to whatever opens the store first.

        A configuration that says ``sqlite`` and a file that is not there yet look the
        same to an operator as a mistake does.
        """
        monkeypatch.chdir(tmp_path)

        _init()

        settings = load_storage_settings()
        database = settings.database_for("fresh-project")
        assert database.exists()
        assert database.parent == default_home() / "databases"

    def test_the_entry_claims_no_migration(self, tmp_path: Path, monkeypatch) -> None:
        """No ``source`` and no ``cutover_at``: nothing was cut over.

        ``source`` is what a rollback reads to find the directory records came from,
        so a value here would name a directory that was never authoritative.
        """
        monkeypatch.chdir(tmp_path)

        _init()

        entry = load_storage_settings().for_project("fresh-project")
        assert entry.source is None
        assert entry.cutover_at is None

    def test_the_config_still_names_a_tasks_directory(self, tmp_path: Path, monkeypatch) -> None:
        """The field stays; the directory does not.

        It is what ``storage cutover`` imports from and what the API's project summary
        reports, so removing it would take the import path with it.
        """
        monkeypatch.chdir(tmp_path)

        _init()

        config = yaml.safe_load((tmp_path / ".agentjobs" / "config.yaml").read_text("utf-8"))
        assert config["tasks_directory"] == "tasks"

    def test_the_output_says_where_records_live(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)

        result = _init()

        assert "Records live in" in result.output
        assert "fresh-project.db" in result.output


class TestAnExistingCorpusIsNamed:
    """Neither ignored nor absorbed (ac-3)."""

    def _seed(self, tmp_path: Path) -> None:
        tasks = tmp_path / "tasks"
        tasks.mkdir()
        (tasks / "task-001.yaml").write_text("id: task-001\n", encoding="utf-8")
        (tasks / "task-002.yaml").write_text("id: task-002\n", encoding="utf-8")

    def test_it_reports_what_it_found(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        self._seed(tmp_path)

        result = _init()

        assert result.exit_code == 0, result.output
        assert "2 task file(s)" in result.output

    def test_it_names_the_import_command(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        self._seed(tmp_path)

        result = _init()

        assert "agentjobs storage cutover --project fresh-project" in result.output

    def test_it_absorbs_nothing(self, tmp_path: Path, monkeypatch) -> None:
        """The files are left alone and the database starts empty."""
        monkeypatch.chdir(tmp_path)
        self._seed(tmp_path)

        _init()

        assert sorted(p.name for p in (tmp_path / "tasks").glob("*.yaml")) == [
            "task-001.yaml",
            "task-002.yaml",
        ]
        result = runner.invoke(app, ["storage", "status"], catch_exceptions=False)
        assert "rows=0 files=2" in result.output

    def test_silence_when_there_is_no_corpus(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)

        result = _init()

        assert "task file(s) already in" not in result.output


class TestTheFileBackendIsStillReachable:
    """``--backend files`` is the old behaviour, kept while the backend exists."""

    def test_it_creates_the_directory(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(
            app,
            ["init", "--backend", "files", "--tasks-dir", "tasks"] + INIT[1:],
            catch_exceptions=False,
        )

        assert result.exit_code == 0, result.output
        assert (tmp_path / "tasks").is_dir()

    def test_it_records_nothing(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)

        runner.invoke(
            app,
            ["init", "--backend", "files", "--tasks-dir", "tasks"] + INIT[1:],
            catch_exceptions=False,
        )

        assert load_storage_settings().projects == {}

    def test_an_unknown_backend_is_refused(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, INIT + ["--backend", "postgres"])

        assert result.exit_code == 1
        assert "Unknown backend" in result.output
        assert not (tmp_path / ".agentjobs").exists()


class TestAProjectAlreadyOnFiles:
    """Registered before this change and never cut over: it keeps working, and is told.

    The alternative -- flipping ``ProjectStorage.backend``'s default to sqlite -- would
    point every one of these at an empty database with no diagnosis.
    """

    def test_the_default_for_an_unregistered_project_is_still_files(self) -> None:
        assert ProjectStorage().backend == "files"
        assert load_storage_settings().backend_for("never-seen") == "files"

    def test_status_names_the_cutover_command(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        runner.invoke(
            app,
            ["init", "--backend", "files", "--tasks-dir", "tasks"] + INIT[1:],
            catch_exceptions=False,
        )

        result = runner.invoke(app, ["storage", "status"], catch_exceptions=False)

        assert "still on task files" in result.output
        assert "agentjobs storage cutover --project fresh-project" in result.output


class TestRecordNewProject:
    def test_it_writes_a_sqlite_entry(self, tmp_path: Path) -> None:
        settings = record_new_project("alpha", home=tmp_path)

        assert settings.on_sqlite("alpha")
        assert settings.path.is_file()

    def test_it_leaves_an_existing_entry_alone(self, tmp_path: Path) -> None:
        """A recorded cutover carries a ``source`` a rollback needs. Never overwrite it."""
        record_cutover("alpha", tmp_path / "tasks", home=tmp_path)
        before = load_storage_settings(tmp_path).for_project("alpha")

        record_new_project("alpha", home=tmp_path)

        assert load_storage_settings(tmp_path).for_project("alpha") == before

    def test_it_gives_the_project_a_file_of_its_own(self, tmp_path: Path) -> None:
        settings = record_new_project("alpha", home=tmp_path)

        assert settings.database_for("alpha") == tmp_path / "databases" / "alpha.db"


class TestTheRegistrationIsWhatCarriesTheEntry:
    def test_a_registered_project_is_the_one_recorded(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)

        _init()

        assert [p.id for p in ProjectRegistry().list_projects()] == ["fresh-project"]
        assert list(load_storage_settings().projects) == ["fresh-project"]
