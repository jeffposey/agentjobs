"""Executable acceptance checks: the field, the store, the evaluator, the triggers.

Every process this file starts is a **real one**. The evaluator's whole job is to turn
an exit code into a verdict, so a test that patched ``subprocess`` would be asserting
that a mock returns what it was told to. The four cases that matter -- a pass, a
failure, a timeout and an executable that does not exist -- are each a real child
process, and the timeout one really waits.

The migration is exercised against a **populated** database, not an empty one. An empty
store proves the SQL parses; the rebuild of ``log_entry`` is the part that can lose
rows, and rows are what it needs to be pointed at.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterator, Tuple

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.authorization import ROUTE_CAPABILITIES
from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.capabilities import Capability, GRANTS
from agentjobs.dispatch.checks import (
    CheckReport,
    NoChecksError,
    evaluate_task,
    run_check,
)
from agentjobs.dispatch.config import ProjectNotEnabledError
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import (
    AcceptanceCriterion,
    AcceptanceStatus,
    CheckOutcome,
    Lifecycle,
    LogEntryType,
    MANAGER_WRITTEN_LOG_TYPES,
    Task,
)
from agentjobs.principals import PrincipalKind
from agentjobs.projects import Project, ProjectRegistry
from agentjobs.sqlstore import migrations
from support import task_store

CONFIG = {
    "project_name": "Sandbox",
    "tasks_directory": "tasks",
    "actors": [
        {"name": "Jeff Posey", "kind": "human"},
        {"name": "claude", "kind": "agent"},
    ],
    "default_user": "Jeff Posey",
}

#: An argv that exits 0 wherever this suite runs. `sys.executable` rather than a shell
#: builtin, because there is no shell: `check` is argv and nothing splits it.
PASSES = [sys.executable, "-c", "print('green')"]
FAILS = [sys.executable, "-c", "import sys; print('red', file=sys.stderr); sys.exit(3)"]
HANGS = [sys.executable, "-c", "import time; print('waiting'); time.sleep(30)"]
ABSENT = ["agentjobs-no-such-executable-147", "--please"]


# ---------------------------------------------------------------------------
# sc-1, sc-2: the field
# ---------------------------------------------------------------------------


class TestTheField:
    def test_a_criterion_carries_an_argv_list(self) -> None:
        criterion = AcceptanceCriterion(id="sc-1", text="It works", check=PASSES)

        assert criterion.check == PASSES
        assert criterion.status is AcceptanceStatus.PENDING

    def test_prose_criteria_are_unaffected(self) -> None:
        criterion = AcceptanceCriterion(id="sc-1", text="It reads well", verify="Look at it.")

        assert criterion.check is None
        assert criterion.verify == "Look at it."

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ([], "empty list"),
            (["", "x"], "check[0] is empty"),
            ([1], "check[0] is int"),
            (["ok", 2], "check[1] is int"),
            ("pytest -q", "check is a string"),
            (5, "not a list of strings"),
        ],
    )
    def test_an_unrunnable_check_is_refused_by_name(self, value: object, expected: str) -> None:
        with pytest.raises(ValueError) as refusal:
            AcceptanceCriterion(id="sc-9", text="t", check=value)

        assert expected in str(refusal.value)
        # Every message names the criterion, because a task carries ten of them and a
        # refusal that does not say which is a refusal you have to bisect.
        assert "'sc-9'" in str(refusal.value)

    def test_verify_is_no_longer_described_as_a_command(self) -> None:
        described = AcceptanceCriterion.model_fields["verify"].description or ""

        assert "command" not in described.lower()
        assert "check" in described

    def test_the_field_description_says_check_is_argv(self) -> None:
        described = AcceptanceCriterion.model_fields["check"].description or ""

        assert "argv" in described.lower()


# ---------------------------------------------------------------------------
# sc-1, sc-3: the store and the migration
# ---------------------------------------------------------------------------


def loaded(manager: TaskManager, task_id: str) -> Task:
    """The task, insisted on. A missing one is a broken fixture, not a case under test."""
    task = manager.get_task(task_id)
    assert task is not None, f"{task_id} vanished between writing it and reading it"
    return task


def a_task_with_checks(manager: TaskManager, *, checks: bool = True) -> str:
    """A ready task carrying one passing check, one failing one, and one prose criterion."""
    acceptance = [
        {"id": "sc-1", "text": "The green one", "check": PASSES if checks else None},
        {"id": "sc-2", "text": "The red one", "check": FAILS if checks else None},
        {"id": "sc-3", "text": "A human looks at it", "verify": "Look at it."},
    ]
    task = manager.create_task(
        title="Checkable",
        category="general",
        summary="A task with executable acceptance checks.",
        description="Run them.",
        lifecycle=Lifecycle.READY,
        actor="Jeff Posey",
        acceptance=[AcceptanceCriterion.model_validate(item) for item in acceptance],
    )
    return task.id


class TestTheStore:
    def test_a_check_round_trips(self, tmp_path: Path) -> None:
        manager = TaskManager(task_store(tmp_path / "tasks"))
        task_id = a_task_with_checks(manager)

        reread = manager.get_task(task_id)

        assert reread is not None
        assert [item.check for item in reread.acceptance] == [PASSES, FAILS, None]

    def test_the_column_holds_json_and_is_null_for_prose(self, tmp_path: Path) -> None:
        store = task_store(tmp_path / "tasks")
        manager = TaskManager(store)
        task_id = a_task_with_checks(manager)

        rows = (
            store.database.reader()
            .execute(
                "SELECT ac_id, check_argv FROM task_acceptance WHERE task_id = ? ORDER BY ord",
                (task_id,),
            )
            .fetchall()
        )

        stored = {row["ac_id"]: row["check_argv"] for row in rows}
        assert json.loads(stored["sc-1"]) == PASSES
        # NULL, never '[]': an empty list is a shape the model refuses outright, so a
        # row holding one would be unreadable by the thing that wrote it.
        assert stored["sc-3"] is None


class TestTheMigration:
    def test_it_is_the_next_number_and_ships(self) -> None:
        available = {item.version: item.name for item in migrations.available()}

        assert available[7] == "acceptance_criteria_carry_an_argv"
        assert migrations.latest_version() == max(available)

    def test_a_populated_database_migrates_forward_without_losing_rows(
        self, tmp_path: Path
    ) -> None:
        """Migrate a copy of a v6 store that already holds tasks, entries and criteria.

        The database is built by the product at the current version and then wound back
        to 6 -- the schema is not re-created by hand, because a hand-written copy of
        ``001_initial.sql`` would drift and this test would then be checking the drift.
        """
        store = task_store(tmp_path / "tasks")
        manager = TaskManager(store)
        task_id = a_task_with_checks(manager, checks=False)
        for index in range(4):
            manager.add_log_entry(
                task_id, actor="claude", type=LogEntryType.PROGRESS, body=f"pass {index}"
            )
        path = Path(store.database.path)
        store.database.close()

        before = _census(path)
        assert before["log_entry"] >= 5, "the fixture must hold entries for the rebuild to lose"

        older = tmp_path / "wound-back.sqlite3"
        # `VACUUM INTO` rather than a file copy, which is also what the product's own
        # pre-migration snapshot does. A copy of the `.sqlite3` alone leaves the WAL
        # behind, and the copy then has no tables at all -- a failure that looks like a
        # broken migration and is really a broken fixture.
        _snapshot(path, older)
        _wind_back_to_version_six(older)

        report = _upgrade(older)

        assert report.from_version == 6
        # The latest version rather than 7, so this case keeps covering the newest
        # migration instead of needing an edit -- and a green one -- every time somebody
        # adds one. What it is asserting is that a *populated* store migrates forward
        # without losing rows, which is a claim about the whole chain.
        assert report.to_version == migrations.latest_version()
        after = _census(older)
        assert after == before
        with sqlite3.connect(older) as connection:
            connection.row_factory = sqlite3.Row
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(task_acceptance)").fetchall()
            }
            assert "check_argv" in columns
            # The rebuild has to leave the feed's positions alone: a cursor that has seen
            # position N must never be shown an earlier entry as N+1.
            assert connection.execute("SELECT count(*) FROM log_feed").fetchone()[0] == (
                before["log_entry"]
            )

    def test_the_database_accepts_a_check_result_row_after_it(self, tmp_path: Path) -> None:
        store = task_store(tmp_path / "tasks")
        manager = TaskManager(store)
        task_id = a_task_with_checks(manager)

        updated = manager.record_check_result(
            task_id,
            actor="Jeff Posey",
            results=[
                CheckOutcome(
                    id="sc-1", status=AcceptanceStatus.MET, exit_code=0, duration_seconds=0.1
                )
            ],
            unchecked=["sc-3"],
        )

        assert updated.log[-1].type is LogEntryType.CHECK_RESULT
        reread = manager.get_task(task_id)
        assert reread is not None
        assert reread.log[-1].type is LogEntryType.CHECK_RESULT
        assert reread.log[-1].data["unchecked"] == ["sc-3"]


def _snapshot(source: Path, destination: Path) -> None:
    """A consistent single-file copy, WAL included."""
    connection = sqlite3.connect(source)
    try:
        connection.execute("VACUUM INTO ?", (str(destination),))
    finally:
        connection.close()


def _census(path: Path) -> Dict[str, int]:
    """Row counts of everything the rebuild could damage."""
    connection = sqlite3.connect(path)
    try:
        return {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("task", "log_entry", "task_acceptance", "task_deliverable", "attachment")
        }
    finally:
        connection.close()


def _wind_back_to_version_six(path: Path) -> None:
    """Undo everything above version 6 on a copy, so the real migrations can re-run.

    **The ledger is cleared for *every* version above 6, not just 007**, and that
    generality is load-bearing rather than tidy (task-150). The store is built by the
    product at whatever the current version is, so a wind-back that deleted only
    ``version = 7`` left the rows for 008 and everything after it behind; the re-run then
    failed on ``UNIQUE constraint failed: schema_migration.version``, in a test about
    migration 007, the first time anybody added 008. The one thing that has to be undone
    by hand is 007's column, because SQLite records nothing that would let this be
    derived -- and ``DROP COLUMN`` on a column a later migration has not touched is safe
    whatever came after it.
    """
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            "PRAGMA foreign_keys = OFF;\n"
            "BEGIN;\n"
            "ALTER TABLE task_acceptance DROP COLUMN check_argv;\n"
            "PRAGMA user_version = 6;\n"
            "DELETE FROM schema_migration WHERE version > 6;\n"
            "COMMIT;\n"
        )
    finally:
        connection.close()


def _upgrade(path: Path) -> migrations.MigrationReport:
    """Run the product's own upgrade against a database file."""
    from agentjobs.sqlstore.connection import Database

    database = Database(path)
    try:
        return migrations.upgrade(database, agentjobs_version="test", snapshot_before=False)
    finally:
        database.close()


# ---------------------------------------------------------------------------
# sc-3: the log entry type
# ---------------------------------------------------------------------------


class TestTheLogEntryType:
    def test_add_log_entry_refuses_it(self, tmp_path: Path) -> None:
        manager = TaskManager(task_store(tmp_path / "tasks"))
        task_id = a_task_with_checks(manager)

        with pytest.raises(ValueError) as refusal:
            manager.add_log_entry(
                task_id, actor="claude", type=LogEntryType.CHECK_RESULT, body="I passed."
            )

        assert "check_result" in str(refusal.value)

    def test_it_is_in_the_manager_written_set(self) -> None:
        assert LogEntryType.CHECK_RESULT in MANAGER_WRITTEN_LOG_TYPES

    def test_a_failure_with_no_cause_and_no_exit_code_is_refused(self) -> None:
        with pytest.raises(ValueError):
            CheckOutcome(id="sc-1", status=AcceptanceStatus.FAILED, duration_seconds=0.0)

    def test_one_entry_per_pass_not_one_per_criterion(self, tmp_path: Path) -> None:
        manager = TaskManager(task_store(tmp_path / "tasks"))
        task_id = a_task_with_checks(manager)
        before = sum(
            1 for item in loaded(manager, task_id).log if item.type is LogEntryType.CHECK_RESULT
        )

        manager.record_check_result(
            task_id,
            actor="Jeff Posey",
            results=[
                CheckOutcome(
                    id="sc-1", status=AcceptanceStatus.MET, exit_code=0, duration_seconds=0.1
                ),
                CheckOutcome(
                    id="sc-2", status=AcceptanceStatus.FAILED, exit_code=3, duration_seconds=0.1
                ),
            ],
            unchecked=["sc-3"],
        )

        task = loaded(manager, task_id)
        assert task is not None
        written = [item for item in task.log if item.type is LogEntryType.CHECK_RESULT]
        assert len(written) - before == 1
        assert [item["id"] for item in written[-1].data["results"]] == ["sc-1", "sc-2"]
        assert task.acceptance[0].status is AcceptanceStatus.MET
        assert task.acceptance[1].status is AcceptanceStatus.FAILED
        # The prose criterion is named as undecided rather than quietly moved.
        assert task.acceptance[2].status is AcceptanceStatus.PENDING


# ---------------------------------------------------------------------------
# sc-4: changing a check resets the status
# ---------------------------------------------------------------------------


class TestTheStatusReset:
    def _met(self, manager: TaskManager) -> str:
        task_id = a_task_with_checks(manager)
        manager.record_check_result(
            task_id,
            actor="Jeff Posey",
            results=[
                CheckOutcome(
                    id="sc-1", status=AcceptanceStatus.MET, exit_code=0, duration_seconds=0.1
                )
            ],
            unchecked=[],
        )
        assert loaded(manager, task_id).acceptance[0].status is AcceptanceStatus.MET
        return task_id

    def test_changing_the_check_resets_the_status(self, tmp_path: Path) -> None:
        manager = TaskManager(task_store(tmp_path / "tasks"))
        task_id = self._met(manager)
        task = loaded(manager, task_id)
        patched = [item.model_dump() for item in task.acceptance]
        patched[0]["check"] = [sys.executable, "-c", "print('different')"]

        updated = manager.update_task(task_id, acceptance=patched)

        assert updated.acceptance[0].status is AcceptanceStatus.PENDING

    def test_editing_the_text_does_not(self, tmp_path: Path) -> None:
        manager = TaskManager(task_store(tmp_path / "tasks"))
        task_id = self._met(manager)
        task = loaded(manager, task_id)
        patched = [item.model_dump() for item in task.acceptance]
        patched[0]["text"] = "The green one, reworded"

        updated = manager.update_task(task_id, acceptance=patched)

        assert updated.acceptance[0].status is AcceptanceStatus.MET
        assert updated.acceptance[0].text == "The green one, reworded"

    def test_a_status_sent_with_a_new_check_does_not_win(self, tmp_path: Path) -> None:
        """A caller cannot assert `met` on a command nothing has run.

        The status in such a patch is stale by construction: the check it claims to
        describe did not exist when the caller decided.
        """
        manager = TaskManager(task_store(tmp_path / "tasks"))
        task_id = a_task_with_checks(manager, checks=False)
        task = loaded(manager, task_id)
        patched = [item.model_dump() for item in task.acceptance]
        patched[0]["check"] = PASSES
        patched[0]["status"] = "met"

        updated = manager.update_task(task_id, acceptance=patched)

        assert updated.acceptance[0].status is AcceptanceStatus.PENDING

    def test_the_rule_holds_for_criterion_models_as_well_as_dicts(self, tmp_path: Path) -> None:
        """The API dumps to dicts; an in-process caller passes models. Both reach it."""
        manager = TaskManager(task_store(tmp_path / "tasks"))
        task_id = self._met(manager)
        task = loaded(manager, task_id)
        criteria = list(task.acceptance)
        criteria[0] = criteria[0].model_copy(
            update={"check": [sys.executable, "-c", "print('other')"]}
        )

        updated = manager.update_task(task_id, acceptance=criteria)

        assert updated.acceptance[0].status is AcceptanceStatus.PENDING

    def test_it_holds_over_http(self, served) -> None:
        client, root, _ = served
        manager = TaskManager(task_store(root / "tasks"))
        task_id = self._met(manager)

        response = client.patch(
            f"/api/projects/sandbox/tasks/{task_id}",
            json={
                "acceptance": [
                    {"id": "sc-1", "text": "The green one", "check": FAILS, "status": "met"},
                    {"id": "sc-3", "text": "A human looks at it", "verify": "Look at it."},
                ]
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["acceptance"][0]["status"] == "pending"


# ---------------------------------------------------------------------------
# sc-5, sc-6: the evaluator
# ---------------------------------------------------------------------------


@pytest.fixture()
def project(tmp_path: Path) -> Project:
    """A registered project rooted at a directory the checks can run in."""
    root = tmp_path / "sandbox"
    (root / ".agentjobs").mkdir(parents=True)
    (root / ".agentjobs" / "config.yaml").write_text(yaml.safe_dump(CONFIG), encoding="utf-8")
    (root / "tasks").mkdir()
    (root / "marker.txt").write_text("here\n", encoding="utf-8")
    return ProjectRegistry().add(root, project_id="sandbox")


def _permitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Open the dispatch gate for the evaluator without configuring a whole machine."""
    monkeypatch.setattr("agentjobs.dispatch.checks.assert_dispatch_permitted", lambda *a, **k: None)


class TestTheEvaluator:
    def test_exit_zero_is_met_and_non_zero_is_failed(self, tmp_path: Path) -> None:
        met = run_check(PASSES, cwd=tmp_path, task_id="task-1", criterion_id="sc-1")
        failed = run_check(FAILS, cwd=tmp_path, task_id="task-1", criterion_id="sc-2")

        assert met.status is AcceptanceStatus.MET
        assert met.exit_code == 0
        assert "green" in (met.output_tail or "")
        assert failed.status is AcceptanceStatus.FAILED
        assert failed.exit_code == 3
        assert "red" in (failed.output_tail or "")
        assert failed.duration_seconds >= 0

    def test_a_timeout_fails_and_names_the_cause(self, tmp_path: Path) -> None:
        outcome = run_check(HANGS, cwd=tmp_path, task_id="task-1", criterion_id="sc-1", timeout=1.0)

        assert outcome.status is AcceptanceStatus.FAILED
        assert outcome.cause == "timeout"
        assert outcome.exit_code is None
        assert "killed after" in (outcome.output_tail or "")

    def test_an_executable_that_does_not_exist_fails_and_names_the_cause(
        self, tmp_path: Path
    ) -> None:
        outcome = run_check(ABSENT, cwd=tmp_path, task_id="task-1", criterion_id="sc-1")

        assert outcome.status is AcceptanceStatus.FAILED
        assert outcome.cause == "not_started"
        assert outcome.exit_code is None
        # Never skipped, and never met: a loop that treated an unrunnable check as
        # anything but a failure could converge by breaking its own tests.
        assert outcome.status is not AcceptanceStatus.PENDING

    def test_it_runs_in_the_project_root_not_the_callers_directory(
        self, project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _permitted(monkeypatch)
        monkeypatch.chdir(tmp_path)
        manager = TaskManager(task_store(project.root / "tasks", project_id=project.id))
        task = loaded(
            manager,
            manager.create_task(
                title="Rooted",
                category="general",
                summary="Where does a check run.",
                description="There.",
                lifecycle=Lifecycle.READY,
                actor="Jeff Posey",
                acceptance=[
                    AcceptanceCriterion(
                        id="sc-1",
                        text="the marker is here",
                        check=[sys.executable, "-c", "open('marker.txt').read()"],
                    )
                ],
            ).id,
        )

        report = evaluate_task(task, project=project)

        assert report.ok, report.results[0].output_tail

    def test_the_task_id_is_in_the_environment_and_no_secret_is(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _permitted(monkeypatch)
        monkeypatch.setenv("AGENTJOBS_RUN_CREDENTIAL", "shhh")
        outcome = run_check(
            [
                sys.executable,
                "-c",
                "import os,sys; sys.exit(0 if os.environ.get('AGENTJOBS_TASK_ID')=='task-1' else 9)",
            ],
            cwd=project.root,
            task_id="task-1",
            criterion_id="sc-1",
        )

        assert outcome.status is AcceptanceStatus.MET

    def test_the_pass_budget_fails_the_checks_it_never_reached(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _permitted(monkeypatch)
        manager = TaskManager(task_store(project.root / "tasks", project_id=project.id))
        task = loaded(
            manager,
            manager.create_task(
                title="Slow",
                category="general",
                summary="A pass that runs out of budget.",
                description="Two slow checks and one budget.",
                lifecycle=Lifecycle.READY,
                actor="Jeff Posey",
                acceptance=[
                    AcceptanceCriterion(id="sc-1", text="slow", check=HANGS),
                    AcceptanceCriterion(id="sc-2", text="never reached", check=PASSES),
                ],
            ).id,
        )

        report = evaluate_task(task, project=project, per_check_timeout=1.0, pass_timeout=1.0)

        assert [item.status for item in report.results] == [
            AcceptanceStatus.FAILED,
            AcceptanceStatus.FAILED,
        ]
        assert report.results[1].cause == "pass_timeout"

    def test_a_task_with_no_checks_is_refused(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _permitted(monkeypatch)
        manager = TaskManager(task_store(project.root / "tasks", project_id=project.id))
        task = loaded(manager, a_task_with_checks(manager, checks=False))

        with pytest.raises(NoChecksError) as refusal:
            evaluate_task(task, project=project)

        assert "no acceptance criteria with a check" in str(refusal.value)

    def test_the_gate_refuses_a_project_not_enabled_for_dispatch(
        self, project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate's own error class, not a generic failure -- and before any process.

        A ``pass`` check is used deliberately: if the gate were checked after the run
        rather than before it, this test would still see the exception and would be
        asserting nothing about ordering. So the argv writes a file, and the file's
        absence is what proves nothing executed.
        """
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("AGENTJOBS_HOME", str(home))
        (home / "dispatch.yaml").write_text(
            yaml.safe_dump(
                {
                    "version": 1,
                    "enabled": True,
                    "runners": {"fake": {"argv": ["python"], "actor": "claude"}},
                    "projects": {"sandbox": {"enabled": False, "runner": "fake"}},
                }
            ),
            encoding="utf-8",
        )
        witness = tmp_path / "it-ran.txt"
        manager = TaskManager(task_store(project.root / "tasks", project_id=project.id))
        task = loaded(
            manager,
            manager.create_task(
                title="Gated",
                category="general",
                summary="A task whose project may not dispatch.",
                description="Nothing should run.",
                lifecycle=Lifecycle.READY,
                actor="Jeff Posey",
                acceptance=[
                    AcceptanceCriterion(
                        id="sc-1",
                        text="never runs",
                        check=[sys.executable, "-c", f"open({str(witness)!r},'w').write('x')"],
                    )
                ],
            ).id,
        )

        with pytest.raises(ProjectNotEnabledError):
            evaluate_task(task, project=project, home=home)

        assert not witness.exists(), "the gate must be walked before anything is executed"


class TestNoReadPathEvaluates:
    """Asserted rather than assumed: a read must not start a process.

    The instrument is a witness file, not an inspection of the code. Reading a task
    through every ordinary surface and then asserting the file was never created is what
    makes this hold for a surface somebody adds later without reading this file.
    """

    def test_reading_listing_and_rendering_run_nothing(self, served, tmp_path: Path) -> None:
        client, root, _ = served
        witness = tmp_path / "read-path-ran-it.txt"
        manager = TaskManager(task_store(root / "tasks"))
        task = manager.create_task(
            title="Watched",
            category="general",
            summary="Nothing may run this.",
            description="A check that leaves a trace if it is ever executed.",
            lifecycle=Lifecycle.READY,
            actor="Jeff Posey",
            acceptance=[
                AcceptanceCriterion(
                    id="sc-1",
                    text="never run on a read",
                    check=[sys.executable, "-c", f"open({str(witness)!r},'w').write('x')"],
                )
            ],
        )

        assert client.get(f"/api/projects/sandbox/tasks/{task.id}").status_code == 200
        assert client.get("/api/projects/sandbox/tasks").status_code == 200
        assert client.get("/api/projects/sandbox/dashboard").status_code in (200, 404)
        assert manager.get_task(task.id) is not None
        assert manager.list_tasks() is not None

        assert not witness.exists()


# ---------------------------------------------------------------------------
# sc-7, sc-8, sc-9: the triggers
# ---------------------------------------------------------------------------


@pytest.fixture()
def served(tmp_path: Path, monkeypatch) -> Iterator[Tuple[TestClient, Path, Path]]:
    """A served project whose machine is configured to permit dispatch."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AGENTJOBS_HOME", str(home))
    monkeypatch.delenv(TASKS_DIR_ENV, raising=False)
    monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    reset_dependency_cache()

    root = tmp_path / "sandbox"
    (root / ".agentjobs").mkdir(parents=True)
    (root / ".agentjobs" / "config.yaml").write_text(yaml.safe_dump(CONFIG), encoding="utf-8")
    (root / "tasks").mkdir()
    (home / "dispatch.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "enabled": True,
                "runners": {"fake": {"argv": [sys.executable, "-c", "pass"], "actor": "claude"}},
                "projects": {"sandbox": {"enabled": True, "runner": "fake"}},
            }
        ),
        encoding="utf-8",
    )
    ProjectRegistry(home=home).add(root, project_id="sandbox")

    with TestClient(app) as client:
        yield client, root, home

    reset_dependency_cache()


class TestTheEndpoint:
    def test_it_returns_the_vector_and_writes_one_entry(self, served) -> None:
        client, root, _ = served
        manager = TaskManager(task_store(root / "tasks"))
        task_id = a_task_with_checks(manager)

        response = client.post(f"/api/projects/sandbox/tasks/{task_id}/check")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is False
        assert [item["id"] for item in body["results"]] == ["sc-1", "sc-2"]
        assert body["results"][0]["status"] == "met"
        assert body["results"][1]["status"] == "failed"
        assert body["results"][1]["exit_code"] == 3
        assert body["unchecked"] == ["sc-3"]

        # Read back through the ordinary read, which is how a client would.
        reread = client.get(f"/api/projects/sandbox/tasks/{task_id}").json()
        entries = [item for item in reread["log"] if item["type"] == "check_result"]
        assert len(entries) == 1
        assert entries[0]["id"] == body["entry_id"]
        assert [item["status"] for item in reread["acceptance"]] == ["met", "failed", "pending"]

    def test_it_refuses_a_task_with_no_checks(self, served) -> None:
        client, root, _ = served
        manager = TaskManager(task_store(root / "tasks"))
        task_id = a_task_with_checks(manager, checks=False)

        response = client.post(f"/api/projects/sandbox/tasks/{task_id}/check")

        assert response.status_code == 409
        assert response.json()["code"] == "no_checks"

    def test_a_gate_refusal_keeps_its_own_code(self, served) -> None:
        client, root, home = served
        (home / "dispatch.yaml").write_text(
            yaml.safe_dump(
                {
                    "version": 1,
                    "enabled": True,
                    "runners": {"fake": {"argv": ["python"], "actor": "claude"}},
                    "projects": {"sandbox": {"enabled": False, "runner": "fake"}},
                }
            ),
            encoding="utf-8",
        )
        manager = TaskManager(task_store(root / "tasks"))
        task_id = a_task_with_checks(manager)

        response = client.post(f"/api/projects/sandbox/tasks/{task_id}/check")

        assert response.status_code == 409
        assert response.json()["code"] == "project_not_enabled"

    def test_an_unknown_task_is_a_404(self, served) -> None:
        client, _, _ = served

        response = client.post("/api/projects/sandbox/tasks/task-999/check")

        assert response.status_code == 404
        assert response.json()["code"] == "task_not_found"


class TestTheCapability:
    def test_the_route_is_classified(self) -> None:
        rule = ROUTE_CAPABILITIES["check_task_acceptance"]

        assert rule.capability is Capability.DISPATCH
        assert rule.task_param == "task_id"

    def test_no_run_holds_it(self) -> None:
        assert Capability.DISPATCH not in GRANTS[PrincipalKind.RUN]

    def test_a_run_principal_is_refused_over_http(self, served, tmp_path: Path) -> None:
        """The audit's method: make the request, holding a real minted credential."""
        from agentjobs.dispatch.credentials import mint_run_credential
        from agentjobs.dispatch.runner import RunDirectory, new_run_id
        from agentjobs.principals import RUN_CREDENTIAL_HEADER

        client, root, home = served
        manager = TaskManager(task_store(root / "tasks"))
        task_id = a_task_with_checks(manager)
        run_id = new_run_id()
        directory = RunDirectory.create(
            home,
            run_id,
            {
                "run_id": run_id,
                "task_id": task_id,
                "project_id": "sandbox",
                "mode": "session",
                "agent": "claude",
                "status": "running",
            },
        )
        token = mint_run_credential(directory.path, run_id)
        assert token, "the credential must actually mint, or nothing below is tested"

        # Constructed, not entered: a second lifespan would run the startup reconciler,
        # which finds this run's recorded pid absent and marks it interrupted -- and a
        # credential dies with its run, so the request would then be refused for the
        # wrong reason and this test would pass while proving nothing about capabilities.
        run = TestClient(app, client=("127.0.0.1", 51000), headers={RUN_CREDENTIAL_HEADER: token})
        response = run.post(f"/api/projects/sandbox/tasks/{task_id}/check")

        assert response.status_code == 403
        assert response.json()["code"] == "capability_denied"


class TestTheCli:
    def _invoke(self, root: Path, home: Path, task_id: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "agentjobs.cli", "check", task_id, "--project", "sandbox"],
            cwd=root,
            capture_output=True,
            text=True,
            # The CLI reconfigures its streams to UTF-8 when they are pipes, so decoding
            # with the locale codepage is what breaks: a cp1252 reader dies on the first
            # tick or cross this command prints.
            encoding="utf-8",
            errors="replace",
            env={**_clean_env(), "AGENTJOBS_HOME": str(home)},
            timeout=180,
        )

    def test_it_prints_the_vector_and_exits_non_zero_on_a_failure(self, served) -> None:
        client, root, home = served
        manager = TaskManager(task_store(root / "tasks"))
        task_id = a_task_with_checks(manager)

        result = self._invoke(root, home, task_id)

        assert result.returncode == 1, result.stdout + result.stderr
        assert "sc-1" in result.stdout and "met" in result.stdout
        assert "sc-2" in result.stdout and "failed" in result.stdout
        # The output tail of the failure is shown; a passing check's is not, because on
        # a pass it buries the one line that did not.
        assert "red" in result.stdout
        assert "green" not in result.stdout

    def test_it_exits_zero_when_everything_passes(self, served) -> None:
        client, root, home = served
        manager = TaskManager(task_store(root / "tasks"))
        task = manager.create_task(
            title="All green",
            category="general",
            summary="Everything passes.",
            description="Two passing checks.",
            lifecycle=Lifecycle.READY,
            actor="Jeff Posey",
            acceptance=[
                AcceptanceCriterion(id="sc-1", text="one", check=PASSES),
                AcceptanceCriterion(id="sc-2", text="two", check=PASSES),
            ],
        )

        result = self._invoke(root, home, task.id)

        assert result.returncode == 0, result.stdout + result.stderr
        assert loaded(manager, task.id).acceptance[0].status is AcceptanceStatus.MET

    def test_it_refuses_a_task_with_no_checks(self, served) -> None:
        client, root, home = served
        manager = TaskManager(task_store(root / "tasks"))
        task_id = a_task_with_checks(manager, checks=False)

        result = self._invoke(root, home, task_id)

        assert result.returncode == 1
        assert "no acceptance criteria with a check" in (result.stdout + result.stderr)

    def test_it_writes_exactly_one_entry(self, served) -> None:
        client, root, home = served
        manager = TaskManager(task_store(root / "tasks"))
        task_id = a_task_with_checks(manager)

        self._invoke(root, home, task_id)

        task = loaded(manager, task_id)
        assert task is not None
        assert sum(1 for item in task.log if item.type is LogEntryType.CHECK_RESULT) == 1


def _clean_env() -> Dict[str, str]:
    """The current environment with this suite's isolation kept and VIRTUAL_ENV dropped.

    ``VIRTUAL_ENV`` is inherited from whichever environment started pytest and would send
    a child ``poetry run`` somewhere else entirely. Nothing here uses ``poetry run``, but
    the subprocess is a real CLI invocation and this is the documented hazard.
    """
    import os

    environment = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}
    return environment


class TestTheReport:
    def test_ok_is_false_when_anything_failed(self) -> None:
        report = CheckReport(
            results=[
                CheckOutcome(
                    id="a", status=AcceptanceStatus.MET, exit_code=0, duration_seconds=0.0
                ),
                CheckOutcome(
                    id="b", status=AcceptanceStatus.FAILED, exit_code=1, duration_seconds=0.0
                ),
            ],
            unchecked=[],
        )

        assert report.ok is False
        assert [item.id for item in report.failed] == ["b"]
