"""The machine-wide live-run surface, ``GET /api/runs/live`` (task-328).

Every other run endpoint filters to one project on purpose. The value of this one is
exactly what those exclude, so the tests that matter here are the cross-project ones:
two projects' runs in one answer, each row carrying the display name and task title of
*its own* project rather than of whichever project the reader happens to be looking at.

Run directories are written by hand rather than dispatched. That is deliberate -- the
states worth asserting on (a batch run whose supervisor is gone, a session parked on a
permission prompt, a meta too broken to parse) are ones a real dispatch cannot be asked
to produce on demand, and the reader under test is a pure function of what is on disk.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Tuple

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.dispatch.ledger import KIND_FINISH, KIND_RUNWAY, locks_root, runway_lock_name
from agentjobs.manager import TaskManager
from agentjobs.projects import ProjectRegistry
from agentjobs.storage import TaskStorage

CONFIG = {
    "tasks_directory": "tasks",
    "actors": [{"name": "Jeff Posey", "kind": "human"}, {"name": "claude", "kind": "agent"}],
    "default_user": "Jeff Posey",
}


def _make_project(tmp_path: Path, home: Path, project_id: str, name: str) -> Path:
    """Register one project with a real tasks directory, and return its root."""
    root = tmp_path / project_id
    (root / ".agentjobs").mkdir(parents=True)
    config = dict(CONFIG, project_name=name)
    (root / ".agentjobs" / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (root / "tasks").mkdir()
    ProjectRegistry(home=home).add(root, project_id=project_id, name=name)
    return root


def _make_task(root: Path, task_id: str, title: str) -> None:
    """One ready task in a project, so a run has a title to resolve."""
    manager = TaskManager(TaskStorage(root / "tasks"))
    manager.create_task(
        id=task_id,
        title=title,
        summary=f"{title}.",
        description=f"{title}, at length.",
        actor="claude",
    )


def _write_run(home: Path, run_id: str, **meta: Any) -> Path:
    """One run directory, exactly as the ledger reads them."""
    directory = home / "runs" / run_id
    directory.mkdir(parents=True)
    body: Dict[str, Any] = {"run_id": run_id, "started_at": _ago(60), **meta}
    (directory / "meta.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")
    return directory


def _write_lock(home: Path, name: str, text: str) -> None:
    """One lock file, in the words ``LockHolder.parse`` reads."""
    directory = locks_root(home)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.lock").write_text(text, encoding="utf-8")


def _ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


@pytest.fixture()
def two_projects(tmp_path: Path, monkeypatch) -> Iterator[Tuple[TestClient, Path, Path, Path]]:
    """A throwaway home serving two registered projects, one task in each."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AGENTJOBS_HOME", str(home))
    monkeypatch.delenv(TASKS_DIR_ENV, raising=False)
    monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    reset_dependency_cache()

    alpha = _make_project(tmp_path, home, "alpha", "Alpha Project")
    beta = _make_project(tmp_path, home, "beta", "Beta Project")
    _make_task(alpha, "task-001", "Teach the queue to count")
    _make_task(beta, "task-500", "Rewrite the importer")

    # The app's lifespan reconciles runs at startup, which would conclude the very
    # records these tests write. The client is therefore built without it: this suite is
    # about the reader, and test_dispatch_lifecycle.py owns reconciliation.
    client = TestClient(app)
    yield client, home, alpha, beta

    reset_dependency_cache()


def _live(client: TestClient) -> Dict[str, Any]:
    response = client.get("/api/runs/live")
    assert response.status_code == 200, response.text
    return response.json()


class TestAcrossProjects:
    """The reason this endpoint is not under /api/projects/{id}."""

    def test_returns_runs_from_every_project_with_their_own_names(self, two_projects):
        client, home, _, _ = two_projects
        _write_run(
            home, "run_a", task_id="task-001", project_id="alpha", mode="session", status="running"
        )
        _write_run(
            home, "run_b", task_id="task-500", project_id="beta", mode="session", status="running"
        )

        body = _live(client)
        rows = {row["run_id"]: row for row in body["runs"]}
        assert set(rows) == {"run_a", "run_b"}
        assert rows["run_a"]["project_name"] == "Alpha Project"
        assert rows["run_b"]["project_name"] == "Beta Project"
        assert rows["run_a"]["task_title"] == "Teach the queue to count"
        assert rows["run_b"]["task_title"] == "Rewrite the importer"

    def test_each_row_links_into_its_own_project(self, two_projects):
        """The link is built server-side because the rows belong to other projects."""
        client, home, _, _ = two_projects
        _write_run(home, "run_b", task_id="task-500", project_id="beta", status="running")

        row = _live(client)["runs"][0]
        assert row["task_url"] == "/p/beta/tasks/task-500"
        assert row["output_url"] == "/api/projects/beta/dispatch/runs/run_b/output"

    def test_the_same_answer_under_every_project_prefix_is_not_offered(self, two_projects):
        """Mounted once. A project-prefixed spelling must 404, not serve a machine answer."""
        client, _, _, _ = two_projects
        assert client.get("/api/projects/alpha/runs/live").status_code == 404


class TestCapacity:
    """The numbers both surfaces render, counted the way the guard counts them."""

    def test_counts_occupied_slots_and_reports_the_ceiling(self, two_projects):
        client, home, _, _ = two_projects
        (home / "dispatch.yaml").write_text(
            yaml.safe_dump({"version": 1, "runners": {}, "limits": {"max_concurrent_runs": 3}}),
            encoding="utf-8",
        )
        _write_run(home, "run_a", task_id="task-001", project_id="alpha", status="running")

        body = _live(client)
        assert body["occupied"] == 1
        assert body["max_concurrent_runs"] == 3
        assert body["dispatch_configured"] is True

    def test_a_machine_with_no_dispatch_config_answers_rather_than_failing(self, two_projects):
        client, _, _, _ = two_projects
        body = _live(client)
        assert body["occupied"] == 0
        assert body["dispatch_configured"] is False

    def test_a_finished_run_leaves_the_list_and_frees_its_slot(self, two_projects):
        client, home, _, _ = two_projects
        directory = _write_run(
            home, "run_a", task_id="task-001", project_id="alpha", status="running"
        )
        assert _live(client)["occupied"] == 1

        meta = yaml.safe_load((directory / "meta.yaml").read_text(encoding="utf-8"))
        meta.update(status="finished", outcome="completed", finished_at=_ago(0))
        (directory / "meta.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")

        body = _live(client)
        assert body["runs"] == []
        assert body["occupied"] == 0


class TestHealth:
    """A live run is not a healthy run, and this surface may not conflate them (ac-5)."""

    def test_a_running_session_is_working(self, two_projects):
        client, home, _, _ = two_projects
        _write_run(
            home, "run_a", task_id="task-001", project_id="alpha", mode="session", status="running"
        )
        assert _live(client)["runs"][0]["health"] == "working"

    def test_a_parked_session_is_not_rendered_as_working(self, two_projects):
        client, home, _, _ = two_projects
        _write_run(
            home, "run_a", task_id="task-001", project_id="alpha", mode="session", status="parked"
        )
        assert _live(client)["runs"][0]["health"] == "parked"

    def test_a_session_that_has_stopped_emitting_is_silent(self, two_projects):
        client, home, _, _ = two_projects
        _write_run(
            home, "run_a", task_id="task-001", project_id="alpha", mode="session", status="stalled"
        )
        assert _live(client)["runs"][0]["health"] == "silent"

    def test_a_batch_run_whose_process_is_gone_is_orphaned(self, two_projects):
        """The rule ``reconcile`` uses, applied without waiting for a restart."""
        client, home, _, _ = two_projects
        _write_run(
            home,
            "run_a",
            task_id="task-001",
            project_id="alpha",
            mode="batch",
            status="running",
            pid=999_999,
        )
        assert _live(client)["runs"][0]["health"] == "orphaned"

    def test_a_session_whose_launcher_pid_is_gone_is_still_working(self, two_projects):
        """A dispatched session outlives the process that started it, by design."""
        client, home, _, _ = two_projects
        _write_run(
            home,
            "run_a",
            task_id="task-001",
            project_id="alpha",
            mode="session",
            status="running",
            pid=999_999,
        )
        assert _live(client)["runs"][0]["health"] == "working"

    def test_an_unreadable_meta_is_unknown_rather_than_working(self, two_projects):
        client, home, _, _ = two_projects
        directory = home / "runs" / "run_a"
        directory.mkdir(parents=True)
        (directory / "meta.yaml").write_text("{not: valid: yaml:", encoding="utf-8")

        row = _live(client)["runs"][0]
        assert row["health"] == "unknown"
        assert row["run_id"] == "run_a"


class TestOtherHolders:
    """Runs are not the only thing occupying this machine (ac-5, second half)."""

    def test_a_scripted_finish_appears_beside_the_runs(self, two_projects):
        client, home, _, _ = two_projects
        _write_lock(
            home,
            "task-001",
            f"pid={os.getpid()} run= kind={KIND_FINISH} finish=fin_abc started={_ago(30)}",
        )

        holders = _live(client)["holders"]
        assert [holder["kind"] for holder in holders] == [KIND_FINISH]
        assert holders[0]["task_id"] == "task-001"
        assert holders[0]["elapsed_seconds"] is not None

    def test_the_merge_runway_names_the_repository_it_is_blocking(self, two_projects):
        client, home, alpha, _ = two_projects
        _write_lock(
            home,
            runway_lock_name(alpha),
            f"pid={os.getpid()} run= kind={KIND_RUNWAY} finish=fin_abc started={_ago(30)}",
        )

        holders = _live(client)["holders"]
        assert [holder["kind"] for holder in holders] == [KIND_RUNWAY]
        assert holders[0]["project_name"] == "Alpha Project"

    def test_a_running_dispatch_is_not_also_listed_as_a_holder(self, two_projects):
        """Its lock is the run, not a second thing happening."""
        client, home, _, _ = two_projects
        _write_run(home, "run_a", task_id="task-001", project_id="alpha", status="running")
        _write_lock(home, "task-001", f"pid={os.getpid()} run=run_a kind=dispatch")

        body = _live(client)
        assert len(body["runs"]) == 1
        assert body["holders"] == []

    def test_a_finish_whose_process_is_gone_is_not_listed(self, two_projects):
        """Same staleness rule as ``release_stale_locks``, without deleting anything."""
        client, home, _, _ = two_projects
        _write_lock(home, "task-001", f"pid=999999 run= kind={KIND_FINISH} finish=fin_abc")
        assert _live(client)["holders"] == []
