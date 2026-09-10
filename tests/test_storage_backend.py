"""Which file a project's records are in, and who is allowed to open it.

There is one kind of store (task-402), so the question this module pins is no longer
*which backend* but *which database* -- and, separately, whether the calling process is
allowed to open it at all. Everything downstream reads the answer from
:mod:`agentjobs.storage_config` and gets its store from :mod:`agentjobs.store_factory`,
so these two are where a mistake would be invisible and total.

The one thing still asked about the retired backend is that a configuration naming it is
**refused by name**: such a project has no readable backlog, and answering from an empty
database would look exactly like a project with no tasks.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Ball, BallReason, Outcome, Priority
from agentjobs.projects import Project
from agentjobs.sqlstore import SqlTaskStore
from agentjobs.storage_config import (
    DATABASE_ENV,
    STORAGE_FILENAME,
    StorageConfigError,
    default_project_database,
    load_storage_settings,
    record_cutover,
)
from agentjobs.store_factory import StoreAccessError, open_store, server_process


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
    """A machine that has never imported anything."""

    def test_no_file_means_no_entries_and_no_configuration_written(self, home: Path) -> None:
        settings = load_storage_settings()
        assert settings.projects == {}
        assert not (home / STORAGE_FILENAME).exists()

    def test_a_project_with_no_entry_gets_a_default_file_of_its_own(self, home: Path) -> None:
        # Not the machine's shared file. An entry naming no database is an operator
        # asking to share; no entry at all says nothing, and the honest default is the
        # same file ``init`` would have given the project.
        resolved = load_storage_settings().database_for("never-heard-of-it")
        assert resolved == default_project_database("never-heard-of-it", home)
        assert resolved != load_storage_settings().database

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
        assert load_storage_settings().for_project("never-heard-of-it").database is None


class TestRecordingAnImport:
    """What ``storage import`` writes."""

    def test_an_import_records_the_time_the_source_and_the_file(
        self, home: Path, project: Project
    ) -> None:
        settings = record_cutover(project.id, project.root / "tasks")
        entry = settings.for_project(project.id)
        assert entry.cutover_at is not None and entry.cutover_at.endswith("Z")
        assert Path(entry.source or "") == (project.root / "tasks").resolve()
        assert Path(entry.database or "") == default_project_database(project.id, home)

    def test_the_entry_names_a_database_and_not_a_backend(
        self, home: Path, project: Project
    ) -> None:
        # A project entry stopped being a choice when there stopped being a choice.
        record_cutover(project.id, project.root / "tasks")
        written = yaml.safe_load((home / STORAGE_FILENAME).read_text(encoding="utf-8"))
        assert "backend" not in written["projects"][project.id]
        assert written["projects"][project.id]["database"]

    def test_one_project_importing_leaves_the_others_alone(self, home: Path) -> None:
        record_cutover("first", Path("/tmp/first"))
        settings = load_storage_settings()
        assert settings.for_project("first").source is not None
        assert settings.for_project("second").source is None


class TestRejectingBadConfiguration:
    """A configuration that cannot be honoured says so rather than defaulting."""

    def test_a_project_still_recorded_on_the_file_backend_is_refused_by_name(
        self, home: Path
    ) -> None:
        # ac-3. Refused rather than ignored: the file backend is gone, so such a
        # project has an *unreadable* backlog, and answering from an empty database
        # would be indistinguishable from a project with no tasks. The message has to
        # name the project and the command that repairs it.
        (home / STORAGE_FILENAME).write_text(
            "projects:\n  demo:\n    backend: files\n  other:\n    backend: files\n",
            encoding="utf-8",
        )
        with pytest.raises(StorageConfigError) as raised:
            load_storage_settings()
        assert "demo" in str(raised.value) and "other" in str(raised.value)
        assert "storage import" in str(raised.value)

    def test_an_entry_that_still_says_sqlite_is_read_without_complaint(self, home: Path) -> None:
        # Every machine configured before task-402 has these, and rewriting somebody's
        # configuration to read it would be a migration nobody asked for.
        (home / STORAGE_FILENAME).write_text(
            "projects:\n  demo:\n    backend: sqlite\n    source: /tmp/demo\n",
            encoding="utf-8",
        )
        assert load_storage_settings().for_project("demo").source == "/tmp/demo"

    def test_a_non_mapping_document_is_refused(self, home: Path) -> None:
        (home / STORAGE_FILENAME).write_text("- not a mapping\n", encoding="utf-8")
        with pytest.raises(StorageConfigError, match="expected a mapping"):
            load_storage_settings()

    def test_an_empty_file_is_the_same_as_no_file(self, home: Path) -> None:
        (home / STORAGE_FILENAME).write_text("", encoding="utf-8")
        assert load_storage_settings().projects == {}


class TestTheFactory:
    """``open_store`` is the only place a project id becomes a store."""

    def test_a_project_gets_a_store_inside_the_server(self, home: Path, project: Project) -> None:
        record_cutover(project.id, project.root / "tasks")
        with server_process():
            store = open_store(project)
            assert isinstance(store, SqlTaskStore)
            assert store.database.path == default_project_database(project.id, home)

    def test_a_project_nobody_has_recorded_still_gets_one(
        self, home: Path, project: Project
    ) -> None:
        # There is no second answer to fall back to any more, so a project with no
        # entry must resolve rather than raise -- and to a file of its own.
        with server_process():
            store = open_store(project)
        assert store.database.path == default_project_database(project.id, home)

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

    def test_each_project_gets_a_database_of_its_own(self, home: Path, tmp_path: Path) -> None:
        # Reversed by task-400. It used to be one file under every project, and the
        # cost was that any decision about one project's records -- publish it, back it
        # up, hand it over, delete it -- was silently a decision about all of them.
        first = Project(id="first", name="First", root=tmp_path / "a")
        second = Project(id="second", name="Second", root=tmp_path / "b")
        (first.root / "tasks").mkdir(parents=True)
        (second.root / "tasks").mkdir(parents=True)
        record_cutover(first.id, first.root / "tasks")
        record_cutover(second.id, second.root / "tasks")
        with server_process():
            one, other = open_store(first), open_store(second)
            assert isinstance(one, SqlTaskStore) and isinstance(other, SqlTaskStore)
            assert one.database is not other.database
            assert one.database.path == home / "databases" / "first.db"
            assert other.database.path == home / "databases" / "second.db"

    def test_projects_told_to_share_a_file_get_one_database(
        self, home: Path, tmp_path: Path
    ) -> None:
        # Still supported, and still the shape of a machine cut over before task-400:
        # one ``Database`` under both stores, because a second write connection on one
        # file would defeat the single-writer rule from inside the server.
        first = Project(id="first", name="First", root=tmp_path / "a")
        second = Project(id="second", name="Second", root=tmp_path / "b")
        (first.root / "tasks").mkdir(parents=True)
        (second.root / "tasks").mkdir(parents=True)
        record_cutover(first.id, first.root / "tasks", shared=True)
        record_cutover(second.id, second.root / "tasks", shared=True)
        with server_process():
            one, other = open_store(first), open_store(second)
            assert isinstance(one, SqlTaskStore) and isinstance(other, SqlTaskStore)
            assert one.database is other.database

    def test_the_declaration_does_not_outlive_the_block(self, home: Path, project: Project) -> None:
        record_cutover(project.id, project.root / "tasks")
        with server_process():
            open_store(project)
        with pytest.raises(StoreAccessError):
            open_store(project)


class TestTheManagerOverTheStore:
    """The verbs behave the same over the store as they did over the files.

    This is the result the whole migration rests on: ``TaskManager`` was written against
    the file backend and was never rewritten. These cases ran against both backends
    until task-402 deleted one of them; they are kept because what they pin is the
    domain layer, not the arm that has gone.
    """

    @pytest.fixture()
    def manager(self, home: Path, project: Project) -> TaskManager:
        record_cutover(project.id, project.root / "tasks")
        with server_process():
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


class TestTheDispatchException:
    """The one family that still opens the store directly, and what bounds it."""

    def test_dispatch_gets_a_local_manager_where_everything_else_goes_remote(
        self, home: Path, project: Project
    ) -> None:
        from agentjobs.cutover import cut_over
        from agentjobs.manager import TaskManager
        from agentjobs.remote_manager import RemoteTaskManager
        from agentjobs.store_factory import dispatch_manager_for, task_manager_for

        cut_over(project, backfill_git=False)

        # Recording a dispatch means sending argv, which a dispatch schema may not
        # carry, so the dispatch family keeps a direct path. Everything else is a
        # service client. See dispatch_manager_for for the whole argument.
        assert isinstance(dispatch_manager_for(project), TaskManager)
        assert isinstance(task_manager_for(project), RemoteTaskManager)

    def test_it_refuses_a_database_this_build_would_have_to_migrate(
        self, home: Path, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Applying a schema change from a short-lived process, possibly while a server
        # holds the file, is the one thing multi-process SQLite does not make safe.
        from agentjobs.cutover import cut_over
        from agentjobs.sqlstore import migrations
        from agentjobs.store_factory import dispatch_manager_for

        cut_over(project, backfill_git=False)
        monkeypatch.setattr(migrations, "latest_version", lambda: 999)

        with pytest.raises(StoreAccessError, match="physical schema version"):
            dispatch_manager_for(project)
