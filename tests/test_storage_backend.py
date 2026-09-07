"""Which backend a project resolves to, and who is allowed to open the database.

The cutover's first question is the one this module pins: *for this project, on this
machine, is the authority files or the database?* Everything downstream -- the CLI going
over HTTP, the import, the retirement of the task-file Git workflows -- reads the answer
from :mod:`agentjobs.storage_config` and gets its store from
:mod:`agentjobs.store_factory`, so these two are where a mistake would be invisible and
total.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Ball, BallReason, Outcome, Priority
from agentjobs.projects import Project
from agentjobs.sqlstore import SqlTaskStore
from agentjobs.storage import TaskStorage
from agentjobs.storage_config import (
    DATABASE_ENV,
    FILES,
    SQLITE,
    STORAGE_FILENAME,
    StorageConfigError,
    load_storage_settings,
    record_cutover,
    record_rollback,
)
from agentjobs.store_factory import (
    StoreAccessError,
    open_store,
    server_process,
    store_is_sql,
)


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A machine-level AgentJobs home nothing else has written to."""
    machine = tmp_path / "home"
    machine.mkdir()
    monkeypatch.setenv("AGENTJOBS_HOME", str(machine))
    monkeypatch.delenv(DATABASE_ENV, raising=False)
    return machine


@pytest.fixture()
def project(tmp_path: Path) -> Project:
    """A project directory with a tasks folder, registered nowhere."""
    root = tmp_path / "demo"
    (root / "tasks").mkdir(parents=True)
    return Project(id="demo", name="Demo", root=root)


class TestDefaults:
    """A machine that has never run a cutover."""

    def test_no_file_means_every_project_is_on_files(self, home: Path) -> None:
        settings = load_storage_settings()
        assert settings.backend_for("anything") == FILES
        assert not settings.any_on_sqlite
        assert not (home / STORAGE_FILENAME).exists()

    def test_the_database_defaults_beside_the_registry(self, home: Path) -> None:
        # Outside every code worktree by construction, which is the one property the
        # spec states for this path.
        assert load_storage_settings().database == home / "agentjobs.db"

    def test_the_environment_overrides_the_configured_path(
        self, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        load_storage_settings().with_database(home / "configured.db").save()
        elsewhere = tmp_path / "volume" / "tasks.db"
        monkeypatch.setenv(DATABASE_ENV, str(elsewhere))
        assert load_storage_settings().database == elsewhere.resolve()

    def test_an_unregistered_project_resolves_rather_than_raising(self, home: Path) -> None:
        # Asked from inside request handling, so it must not be able to fail.
        assert load_storage_settings().for_project("never-heard-of-it").backend == FILES


class TestRecordingACutover:
    """What ``storage cutover`` and ``storage rollback`` write."""

    def test_cutover_records_the_backend_the_time_and_the_source(
        self, home: Path, project: Project
    ) -> None:
        settings = record_cutover(project.id, project.root / "tasks")
        entry = settings.for_project(project.id)
        assert entry.backend == SQLITE
        assert entry.cutover_at is not None and entry.cutover_at.endswith("Z")
        assert Path(entry.source or "") == (project.root / "tasks").resolve()

    def test_the_written_file_is_what_a_fresh_read_returns(
        self, home: Path, project: Project
    ) -> None:
        record_cutover(project.id, project.root / "tasks")
        assert load_storage_settings().on_sqlite(project.id)
        written = yaml.safe_load((home / STORAGE_FILENAME).read_text(encoding="utf-8"))
        assert written["projects"][project.id]["backend"] == SQLITE

    def test_rollback_keeps_the_entry_so_never_migrated_stays_distinguishable(
        self, home: Path, project: Project
    ) -> None:
        record_cutover(project.id, project.root / "tasks")
        settings = record_rollback(project.id)
        entry = settings.for_project(project.id)
        assert entry.backend == FILES
        # The history is what makes "rolled back" readable as something other than
        # "never touched", which is a different situation to be in.
        assert entry.cutover_at is not None
        assert entry.source is not None

    def test_one_project_migrating_leaves_the_others_alone(self, home: Path) -> None:
        record_cutover("first", Path("/tmp/first"))
        settings = load_storage_settings()
        assert settings.on_sqlite("first")
        assert not settings.on_sqlite("second")


class TestRejectingBadConfiguration:
    """A configuration that cannot be honoured says so rather than defaulting."""

    def test_an_unknown_backend_is_named(self, home: Path) -> None:
        (home / STORAGE_FILENAME).write_text(
            "projects:\n  demo:\n    backend: postgres\n", encoding="utf-8"
        )
        with pytest.raises(StorageConfigError, match="postgres"):
            load_storage_settings()

    def test_a_non_mapping_document_is_refused(self, home: Path) -> None:
        (home / STORAGE_FILENAME).write_text("- not a mapping\n", encoding="utf-8")
        with pytest.raises(StorageConfigError, match="expected a mapping"):
            load_storage_settings()

    def test_an_empty_file_is_the_same_as_no_file(self, home: Path) -> None:
        (home / STORAGE_FILENAME).write_text("", encoding="utf-8")
        assert load_storage_settings().backend_for("demo") == FILES


class TestTheFactory:
    """``open_store`` is the only place a project id becomes a store."""

    def test_a_project_on_files_gets_the_file_backend(self, home: Path, project: Project) -> None:
        store = open_store(project)
        assert isinstance(store, TaskStorage)
        assert not store_is_sql(store)

    def test_a_project_on_sqlite_gets_the_sql_backend_inside_the_server(
        self, home: Path, project: Project
    ) -> None:
        record_cutover(project.id, project.root / "tasks")
        with server_process():
            store = open_store(project)
            assert isinstance(store, SqlTaskStore)
            assert store_is_sql(store)

    def test_a_client_process_is_refused_and_told_where_to_go(
        self, home: Path, project: Project
    ) -> None:
        # The owner's decision of 2026-09-05 is that only the server opens the
        # database. Enforced here rather than written down, because a rule in prose
        # holds until somebody adds a call site in a hurry.
        record_cutover(project.id, project.root / "tasks")
        with pytest.raises(StoreAccessError) as raised:
            open_store(project)
        assert "only the AgentJobs server" in str(raised.value)
        assert "over the service" in str(raised.value)

    def test_one_database_serves_every_project(self, home: Path, tmp_path: Path) -> None:
        first = Project(id="first", name="First", root=tmp_path / "a")
        second = Project(id="second", name="Second", root=tmp_path / "b")
        (first.root / "tasks").mkdir(parents=True)
        (second.root / "tasks").mkdir(parents=True)
        record_cutover(first.id, first.root / "tasks")
        record_cutover(second.id, second.root / "tasks")
        with server_process():
            # One Database under both stores: a second write connection on one file
            # would defeat the single-writer rule from inside the server.
            assert open_store(first).database is open_store(second).database

    def test_the_declaration_does_not_outlive_the_block(self, home: Path, project: Project) -> None:
        record_cutover(project.id, project.root / "tasks")
        with server_process():
            open_store(project)
        with pytest.raises(StoreAccessError):
            open_store(project)


class TestTheManagerOnEitherBackend:
    """The verbs behave the same whichever store is under them.

    This is the result the whole cutover rests on: ``TaskManager`` was written against
    the file backend and is not rewritten. If a verb needed a different implementation
    per backend, the migration would be a rewrite of the domain layer rather than a
    change of where rows live.
    """

    @pytest.fixture(params=[FILES, SQLITE])
    def manager(self, request: pytest.FixtureRequest, home: Path, project: Project) -> TaskManager:
        if request.param == SQLITE:
            record_cutover(project.id, project.root / "tasks")
            with server_process():
                return TaskManager(open_store(project))
        return TaskManager(open_store(project))

    def test_create_promote_claim_handoff_close(self, manager: TaskManager) -> None:
        task = manager.create_task(
            title="Cut over",
            description="Move the corpus",
            actor="claude",
            priority=Priority.HIGH,
        )
        assert task.queue_position is not None

        manager.promote_task(task.id, actor="claude")
        claimed = manager.claim_task(task.id, agent="claude")
        assert claimed.assignment.owner == "claude"

        handed = manager.handoff(
            task.id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Read the diff",
        )
        assert handed.ball is Ball.HUMAN
        assert handed.ball_prompt == "Read the diff"

        closed = manager.close_task(task.id, actor="claude", outcome=Outcome.COMPLETED, body="done")
        assert closed.lifecycle.value == "closed"

    def test_the_queue_hands_out_one_slot_per_band(self, manager: TaskManager) -> None:
        first = manager.create_task(
            title="First", description="d", actor="claude", priority=Priority.HIGH
        )
        second = manager.create_task(
            title="Second", description="d", actor="claude", priority=Priority.HIGH
        )
        assert first.queue_position != second.queue_position
        assert manager.check_queue() == []

    def test_selection_answers_from_the_stored_order(self, manager: TaskManager) -> None:
        first = manager.create_task(
            title="First", description="d", actor="claude", priority=Priority.HIGH
        )
        manager.create_task(title="Second", description="d", actor="claude", priority=Priority.LOW)
        manager.promote_task(first.id, actor="claude")
        chosen = manager.get_next_task()
        assert chosen is not None and chosen.id == first.id

    def test_a_missing_task_is_reported_as_missing(self, manager: TaskManager) -> None:
        from agentjobs.manager import TaskNotFoundError

        with pytest.raises(TaskNotFoundError):
            manager.claim_task("task-999", agent="claude")
