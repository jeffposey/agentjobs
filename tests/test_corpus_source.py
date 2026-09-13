"""The corpus checks run, fail when they cannot, and never write the machine (task-411).

From task-311 until task-411 every check that reads this repository's own backlog skipped
under pytest: `isolate_project_registry` hid the machine's store from each test, the read
answered nothing, and each caller turned nothing into a skip. Nobody noticed for weeks.

These are the two properties that would have noticed:

*   **A store that cannot be read fails every one of those checks** -- unless the machine
    explicitly opts out, in which case the skip says so.
*   **Reading the real store does not loosen the isolation.** The registry a test writes
    is still a temp one, and the database is never opened for writing -- not even by a
    migration.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable, Iterator, List

import pytest

import corpus_source
import test_dispatch_log_entries
import test_task_corpus as corpus_checks
from agentjobs.manager import TaskManager
from agentjobs.projects import ProjectRegistry, default_home
from agentjobs.storage_config import DATABASE_ENV, default_project_database
from support import task_store

BACKLOG_CHECKS: List[Callable[[], None]] = [
    corpus_checks.test_corpus_is_not_empty,
    corpus_checks.test_every_record_carries_the_stamp_and_round_trips,
    corpus_checks.test_agentjobs_task_ids_and_relationships_are_not_dangling,
    corpus_checks.test_agentjobs_context_paths_exist,
    corpus_checks.test_open_ui_tasks_do_not_target_legacy_templates,
    corpus_checks.test_no_task_record_quotes_a_person_verbatim,
    test_dispatch_log_entries.TestNothingElseChanged().test_existing_records_still_load,
]
"""Every check that reads this repository's backlog. A new one belongs here too."""


@pytest.fixture
def machine_home(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[Path], None]]:
    """Point the corpus read at another home for one test, and forget the read after.

    The reset on the way out matters as much as the one on the way in: the read is cached
    per process, so a failure cached here would fail the real checks that run next in
    the same worker.
    """

    def point_at(home: Path) -> None:
        monkeypatch.setattr(corpus_source, "MACHINE_HOME", home)
        corpus_source.reset_cache()

    corpus_source.reset_cache()
    yield point_at
    corpus_source.reset_cache()


def register_agentjobs(home: Path, root: Path) -> None:
    """Register a project called ``agentjobs`` in ``home``'s registry."""
    root.mkdir(parents=True, exist_ok=True)
    ProjectRegistry(home=home).add(root, project_id=corpus_source.PROJECT_ID)


# ----- an unreadable store fails ---------------------------------------------------


@pytest.mark.parametrize("check", BACKLOG_CHECKS, ids=lambda check: check.__name__)
def test_every_backlog_check_fails_when_the_store_cannot_be_read(
    check: Callable[[], None], machine_home, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(corpus_source.OPT_OUT_ENV, raising=False)
    machine_home(tmp_path / "a-home-with-nothing-in-it")

    with pytest.raises(pytest.fail.Exception, match="could not be read"):
        check()


def test_opting_out_skips_and_the_skip_names_the_variable(
    machine_home, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(corpus_source.OPT_OUT_ENV, "1")
    machine_home(tmp_path / "a-home-with-nothing-in-it")

    with pytest.raises(pytest.skip.Exception, match=corpus_source.OPT_OUT_ENV):
        corpus_checks.test_corpus_is_not_empty()


def test_an_unregistered_project_is_named_as_the_reason(tmp_path: Path) -> None:
    with pytest.raises(corpus_source.CorpusUnavailable, match="is not registered"):
        corpus_source.read_backlog(tmp_path)


def test_a_missing_database_is_named_as_the_reason(tmp_path: Path) -> None:
    register_agentjobs(tmp_path / "home", tmp_path / "root")

    with pytest.raises(corpus_source.CorpusUnavailable, match="does not exist"):
        corpus_source.read_backlog(tmp_path / "home")


def test_a_test_that_moved_the_database_override_is_refused(monkeypatch, tmp_path: Path) -> None:
    register_agentjobs(tmp_path / "home", tmp_path / "root")
    monkeypatch.setenv(DATABASE_ENV, str(tmp_path / "somewhere-else.db"))
    monkeypatch.setattr(corpus_source, "MACHINE_DATABASE_OVERRIDE", None)

    with pytest.raises(corpus_source.CorpusUnavailable, match="has been changed"):
        corpus_source.read_backlog(tmp_path / "home")


# ----- a readable store is read ----------------------------------------------------


def test_a_populated_store_is_read_through_the_captured_home(
    machine_home, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(DATABASE_ENV, raising=False)
    monkeypatch.setattr(corpus_source, "MACHINE_DATABASE_OVERRIDE", None)
    home = default_home()
    register_agentjobs(home, tmp_path / "root")
    store = task_store(tmp_path / "root" / "tasks", project_id=corpus_source.PROJECT_ID)
    TaskManager(store).create_task(title="One record", description="So there is a backlog.")
    machine_home(home)

    tasks = corpus_source.backlog()

    assert [task.title for task in tasks] == ["One record"]


# ----- the machine is never written ------------------------------------------------


def test_the_machine_home_was_captured_before_the_isolation_fixture_moved_it() -> None:
    """If this fails, the capture is happening too late and reads the temp home."""
    assert default_home() != corpus_source.MACHINE_HOME
    assert not ProjectRegistry().path.is_relative_to(corpus_source.MACHINE_HOME)


def test_a_registration_inside_a_test_never_reaches_the_machine_registry(
    tmp_path: Path,
) -> None:
    """Shown, not asserted: read the real backlog, register a project, compare bytes.

    The guard before the write is what keeps this test from being the incident it checks
    for -- if isolation were broken it fails there, having written nothing.
    """
    machine_registry = ProjectRegistry(home=corpus_source.MACHINE_HOME).path
    before = machine_registry.read_bytes() if machine_registry.is_file() else None

    try:
        corpus_source.read_backlog(corpus_source.MACHINE_HOME)
    except corpus_source.CorpusUnavailable:
        pass  # a machine with no store still has a registry worth protecting
    registry = ProjectRegistry()
    assert registry.path != machine_registry, "isolation is broken; refusing to write"
    (tmp_path / "project").mkdir()
    registry.add(tmp_path / "project", project_id="isolation-probe")

    after = machine_registry.read_bytes() if machine_registry.is_file() else None
    assert after == before
    assert "isolation-probe" in registry.as_dict()


def test_reading_a_store_never_migrates_or_writes_the_source(tmp_path: Path) -> None:
    """The read goes through a snapshot, because opening the file would migrate it.

    A database at schema zero stands in for the live store a branch carrying a newer
    migration would otherwise rewrite on open.
    """
    home = tmp_path / "home"
    register_agentjobs(home, tmp_path / "root")
    source = default_project_database(corpus_source.PROJECT_ID, home)
    source.parent.mkdir(parents=True)
    connection = sqlite3.connect(str(source))
    connection.execute("CREATE TABLE sentinel (value TEXT)")
    connection.commit()
    connection.close()
    before = source.read_bytes()

    with pytest.raises(corpus_source.CorpusUnavailable, match="holds no"):
        corpus_source.read_backlog(home)

    assert source.read_bytes() == before
    connection = sqlite3.connect(str(source))
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()
    assert tables == {"sentinel"}
    assert version == 0
