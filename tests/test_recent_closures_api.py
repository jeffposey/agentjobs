"""What just finished, machine-wide: ``GET /api/recent/closures`` (task-460).

The Dashboard could already say what is running and what needs a person. This is the
third question -- *what landed while I was not looking* -- and three properties decide
whether the answer is worth putting on a page somebody reads from a phone:

*   **It is cross-project.** Work lands unattended in whichever project the walk was
    dispatched in, so a region that could only see the open project would miss most of
    what it exists to report. That is why the tests here seed two projects.
*   **It is ordered by the close, not by the last edit.** The two differ exactly when a
    closed record is edited afterwards -- a correction, a redaction, a late decision
    entry -- and that is common enough that ``updated`` would routinely re-date an old
    closure to this morning. There is a test with precisely that shape.
*   **A project the caller may not see contributes nothing.** A row carries a task id, a
    title and a project name, so a single leaked row is three disclosures.

``closed_at`` is backdated through :func:`support.set_closed_at` rather than produced by
waiting, for the reason ``set_updated`` exists: the verb stamps the moment it runs, so a
test about a *window* cannot make its own fixtures through the verb.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.closures import RECENT_LIMIT, RECENT_WINDOW_DAYS
from agentjobs.exposure import VISIBILITY_KEY
from agentjobs.front_door import SECRET_ENV, reset_cache as reset_front_door_cache
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Outcome
from agentjobs.principals import (
    FRONT_DOOR_HEADER,
    IDENTITY_HEADER,
    RUN_CREDENTIAL_HEADER,
    RunCredential,
    reset_run_credential_verifier,
    set_run_credential_verifier,
)
from agentjobs.projects import ProjectRegistry
from agentjobs.sqlstore import SqlTaskStore
from support import set_closed_at, set_updated, task_store

LOOPBACK = "127.0.0.1"

FRONT_DOOR_SECRET = "a-front-door-secret-for-this-test"
"""Since task-244 loopback alone is not the front door, so a client that sets only the
identity header resolves to *nobody* -- which would make the refusals below pass for the
wrong reason."""

REMOTE_HEADERS = {FRONT_DOOR_HEADER: FRONT_DOOR_SECRET, IDENTITY_HEADER: "jeff@example.com"}

RUN_TOKEN = "a-run-credential-that-verifies"

OPEN_PROJECT = "alpha"
HIDDEN_PROJECT = "ledger"

SECRET = "the rent ledger for 14 Elm Row"
"""The hidden project's task title, so a leak can be asserted on the bytes rather than
on a count."""

CONFIG: Dict[str, Any] = {
    "tasks_directory": "tasks",
    "actors": [{"name": "Jeff Posey", "kind": "human"}, {"name": "claude", "kind": "agent"}],
    "default_user": "Jeff Posey",
}


def _fake_verifier(presented: str):
    """Stand in for a minted credential, as ``test_principals.py`` does."""
    if presented == RUN_TOKEN:
        return RunCredential(run_id="run_43c2a909", task_id="task-460")
    return None


def _make_project(tmp_path: Path, home: Path, project_id: str, **extra: Any) -> Path:
    root = tmp_path / project_id
    (root / ".agentjobs").mkdir(parents=True)
    config = dict(CONFIG, project_name=f"{project_id.title()} Project", **extra)
    (root / ".agentjobs" / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (root / "tasks").mkdir()
    ProjectRegistry(home=home).add(root, project_id=project_id, name=config["project_name"])
    return root


def _store(root: Path) -> SqlTaskStore:
    return task_store(root / "tasks")


def _close(
    root: Path,
    task_id: str,
    title: str,
    *,
    closed_ago: timedelta,
    outcome: Outcome = Outcome.COMPLETED,
    archive: bool = False,
) -> None:
    """Create a task, close it, and put its closure where the window can see it."""
    store = _store(root)
    manager = TaskManager(store)
    manager.create_task(
        id=task_id,
        title=title,
        summary=f"{title}.",
        description=f"{title}, at length.",
        actor="claude",
    )
    manager.close_task(task_id, actor="claude", outcome=outcome, archive=archive)
    set_closed_at(store, task_id, datetime.now(timezone.utc) - closed_ago)


@pytest.fixture()
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Tuple[Path, Path]]:
    """Two registered projects -- one shared, one ``visibility: local`` -- both with
    closures, and a spread of ages and outcomes across them."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AGENTJOBS_HOME", str(home))
    monkeypatch.setenv(SECRET_ENV, FRONT_DOOR_SECRET)
    reset_front_door_cache()
    monkeypatch.delenv(TASKS_DIR_ENV, raising=False)
    monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    reset_dependency_cache()
    set_run_credential_verifier(_fake_verifier)

    alpha = _make_project(tmp_path, home, OPEN_PROJECT)
    hidden = _make_project(tmp_path, home, HIDDEN_PROJECT, **{VISIBILITY_KEY: "local"})

    _close(alpha, "task-001", "Teach the queue to count", closed_ago=timedelta(hours=1))
    _close(
        alpha,
        "task-002",
        "Replace the Alpine dashboard",
        closed_ago=timedelta(hours=5),
        outcome=Outcome.SUPERSEDED,
    )
    _close(alpha, "task-003", "A closure older than the window", closed_ago=timedelta(days=30))
    _close(hidden, "task-900", SECRET, closed_ago=timedelta(hours=2))

    yield tmp_path, home

    reset_dependency_cache()
    reset_front_door_cache()
    reset_run_credential_verifier()


def owner() -> TestClient:
    """The person at this machine: loopback, no headers at all."""
    return TestClient(app, client=(LOOPBACK, 51002))


def remote() -> TestClient:
    """A tailnet peer: through the proven front door, carrying a proven identity."""
    return TestClient(app, client=(LOOPBACK, 51001), headers=REMOTE_HEADERS)


def run() -> TestClient:
    """A dispatched agent, presenting a run credential that verifies."""
    return TestClient(app, client=(LOOPBACK, 51003), headers={RUN_CREDENTIAL_HEADER: RUN_TOKEN})


def _closures(client: TestClient, **params: Any) -> List[Dict[str, Any]]:
    response = client.get("/api/recent/closures", params=params)
    assert response.status_code == 200, response.text
    body: Dict[str, Any] = response.json()
    rows: List[Dict[str, Any]] = body["closures"]
    return rows


def _ids(rows: List[Dict[str, Any]]) -> List[str]:
    return [row["task_id"] for row in rows]


class TestWhatARowSays:
    """ac-1: id, title, project, outcome and time, each linking to the task."""

    def test_every_field_a_row_renders_is_on_the_wire(self, machine) -> None:
        rows = _closures(owner())
        first = next(row for row in rows if row["task_id"] == "task-001")

        assert first["task_title"] == "Teach the queue to count"
        assert first["project_id"] == OPEN_PROJECT
        assert first["project_name"] == "Alpha Project"
        assert first["outcome"] == "completed"
        assert first["task_url"] == f"/p/{OPEN_PROJECT}/tasks/task-001"
        # An hour ago, give or take however long this test took to run.
        assert 3000 < first["age_seconds"] < 4200

    def test_a_non_completed_outcome_is_said_rather_than_flattened(self, machine) -> None:
        """A region that printed every closure as *done* would hide the one difference
        a reader scans these rows for."""
        rows = _closures(owner())
        superseded = next(row for row in rows if row["task_id"] == "task-002")

        assert superseded["outcome"] == "superseded"

    def test_the_link_points_into_the_row_s_own_project(self, machine) -> None:
        """The rows belong to other projects by design, so the client cannot build these."""
        rows = _closures(owner())

        for row in rows:
            assert row["task_url"] == f"/p/{row['project_id']}/tasks/{row['task_id']}"

    def test_the_bounds_it_was_computed_under_are_on_the_wire(self, machine) -> None:
        """The empty state says the number of days, and must not hard-code it."""
        body = owner().get("/api/recent/closures").json()

        assert body["limit"] == RECENT_LIMIT
        assert body["window_days"] == RECENT_WINDOW_DAYS
        assert body["generated_at"]


class TestAcrossProjects:
    """The reason this endpoint is not under /api/projects/{id}."""

    def test_both_projects_closures_are_in_one_answer(self, machine) -> None:
        rows = _closures(owner())

        assert {row["project_id"] for row in rows} == {OPEN_PROJECT, HIDDEN_PROJECT}

    def test_newest_first_across_the_projects(self, machine) -> None:
        """One hour, two hours, five hours -- and the middle one is the other project's."""
        assert _ids(_closures(owner())) == ["task-001", "task-900", "task-002"]

    def test_each_row_names_its_own_project(self, machine) -> None:
        rows = _closures(owner())
        hidden = next(row for row in rows if row["task_id"] == "task-900")

        assert hidden["project_name"] == "Ledger Project"


class TestTheWindow:
    """What *recently* means, and what falls out of it."""

    def test_a_closure_older_than_the_window_is_absent(self, machine) -> None:
        assert "task-003" not in _ids(_closures(owner()))

    def test_widening_the_window_reaches_it(self, machine) -> None:
        """The thirty-day-old closure is in the store, not merely unreachable."""
        assert "task-003" in _ids(_closures(owner(), days=60))

    def test_nothing_in_the_window_is_an_empty_list_not_an_error(self, machine) -> None:
        """The state the region's empty sentence is for: a quiet stretch, not an error.

        Produced by ageing every closure past the window rather than by an empty store,
        because that is the state a real machine reaches -- the store is full of finished
        work and none of it is news.
        """
        tmp_path = Path(machine[0])
        long_ago = datetime.now(timezone.utc) - timedelta(days=3)
        for project, ids in (
            (OPEN_PROJECT, ["task-001", "task-002"]),
            (HIDDEN_PROJECT, ["task-900"]),
        ):
            store = _store(tmp_path / project)
            for task_id in ids:
                set_closed_at(store, task_id, long_ago)

        body = owner().get("/api/recent/closures", params={"days": 1}).json()

        assert body["closures"] == []
        assert body["window_days"] == 1

    def test_the_limit_bounds_the_merged_answer(self, machine) -> None:
        rows = _closures(owner(), limit=2)

        assert _ids(rows) == ["task-001", "task-900"]

    def test_a_limit_past_the_ceiling_is_refused_rather_than_silently_clamped(
        self, machine
    ) -> None:
        """This is a region, not a history export; the Tasks list is the history.

        400 rather than 422 because this application renders every request-validation
        failure as a 400 (``api/main.py``); the assertion is that it refuses, not that it
        quietly serves five hundred rows.
        """
        assert owner().get("/api/recent/closures", params={"limit": 500}).status_code == 400


class TestTheTimestampIsTheClose:
    """``closed_at``, never ``updated`` -- the decision this module exists to hold."""

    def test_an_edit_after_closing_does_not_re_date_the_closure(self, machine) -> None:
        """A month-old closure corrected this morning stays a month old.

        ``updated`` is what a naive ordering would use and it now says *now*; the row is
        still outside the window, which is the whole claim.
        """
        alpha = Path(machine[0]) / OPEN_PROJECT
        set_updated(_store(alpha), "task-003", datetime.now(timezone.utc))

        assert "task-003" not in _ids(_closures(owner()))

    def test_nor_does_it_reorder_the_rows(self, machine) -> None:
        alpha = Path(machine[0]) / OPEN_PROJECT
        set_updated(_store(alpha), "task-002", datetime.now(timezone.utc))

        assert _ids(_closures(owner())) == ["task-001", "task-900", "task-002"]


class TestArchivedClosuresStillLanded:
    def test_a_task_closed_and_archived_in_one_gesture_is_shown(self, machine) -> None:
        """Archiving says where a task is *listed*; it does not say it did not finish."""
        alpha = Path(machine[0]) / OPEN_PROJECT
        _close(
            alpha,
            "task-004",
            "Filed away on the way out",
            closed_ago=timedelta(minutes=10),
            archive=True,
        )

        assert _ids(_closures(owner()))[0] == "task-004"


class TestExposure:
    """ac-2: a viewer without access to a project sees none of its closures.

    Three callers, because "without access" is a property of the *caller* and only one
    of these three is outside the machine. A run credential is a machine-local principal
    (``exposure.LOCAL_KINDS``) -- a dispatched agent has the files on disk, so refusing
    it over HTTP would protect nothing -- so the run is asserted to see what the owner
    sees, and the tailnet peer is the one that must not.
    """

    def test_a_remote_caller_gets_no_row_from_the_hidden_project(self, machine) -> None:
        rows = _closures(remote())

        assert {row["project_id"] for row in rows} == {OPEN_PROJECT}
        assert "task-900" not in _ids(rows)

    def test_the_hidden_project_s_words_are_not_in_the_body(self, machine) -> None:
        """Asserted on the bytes: a row leaks a title and a project name, not a count."""
        body = remote().get("/api/recent/closures").text

        assert SECRET not in body
        assert "Ledger Project" not in body
        assert HIDDEN_PROJECT not in body

    def test_a_run_credential_sees_what_the_machine_sees(self, machine) -> None:
        assert _ids(_closures(run())) == _ids(_closures(owner()))

    def test_an_owner_loses_nothing(self, machine) -> None:
        """The mistake that hides Jeff's own projects from his own machine fails here."""
        assert "task-900" in _ids(_closures(owner()))
