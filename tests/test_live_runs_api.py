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

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.dispatch.journal import journal
from agentjobs.dispatch.ledger import (
    KIND_DISPATCH,
    KIND_FINISH,
    KIND_RUNWAY,
    locks_root,
    runway_lock_name,
)
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Ball, BallReason, Lifecycle, Outcome
from agentjobs.projects import ProjectRegistry
from support import task_store

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
    manager = TaskManager(task_store(root / "tasks"))
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
    parsed: Dict[str, Any] = response.json()
    return parsed


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

    @pytest.mark.parametrize(
        ("outcome", "word", "category"),
        [
            (Outcome.COMPLETED, "Completed", "closed"),
            (Outcome.CANCELLED, "Cancelled", "closed_unfinished"),
        ],
    )
    def test_a_run_whose_task_closed_carries_the_task_s_own_word(
        self, two_projects, outcome, word, category
    ):
        """task-577: the tile says what the task's chip says, not a word of its own."""
        client, home, alpha, _ = two_projects
        TaskManager(task_store(alpha / "tasks")).close_task(
            "task-001", outcome=outcome, actor="claude"
        )
        _write_run(
            home,
            "run_a",
            task_id="task-001",
            project_id="alpha",
            mode="session",
            status="running",
            slot_released_at=_ago(5),
        )
        row = _live(client)["runs"][0]
        assert row["health"] == "work_done"
        assert (row["task_display_status"], row["task_status_category"]) == (word, category)

    def test_an_open_task_s_run_carries_no_task_word(self, two_projects):
        client, home, _, _ = two_projects
        _write_run(
            home, "run_a", task_id="task-001", project_id="alpha", mode="session", status="running"
        )
        row = _live(client)["runs"][0]
        assert (row["task_display_status"], row["task_status_category"]) == ("", None)

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


def _write_finish(
    home: Path,
    finish_id: str,
    task_id: str,
    project_id: str,
    *,
    steps: Tuple[str, ...] = ("preflight",),
    **meta: Any,
) -> Path:
    """A finish directory with ``steps`` done, as ``FinishDirectory`` leaves one.

    With only ``preflight`` done the step in flight is ``runway``: queued for the merge
    runway, the exact state task-369 was in when task-533 was filed.
    """
    directory = home / "finishes" / finish_id
    directory.mkdir(parents=True, exist_ok=True)
    body: Dict[str, Any] = {
        "finish_id": finish_id,
        "task_id": task_id,
        "project_id": project_id,
        "outcome": "running",
        "started_at": _ago(20),
        **meta,
    }
    (directory / "meta.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")
    with (directory / "phases.jsonl").open("a", encoding="utf-8") as handle:
        for step in steps:
            line = {"ts": _ago(10), "kind": "finish_step", "step": step, "ok": True, "seconds": 1.0}
            handle.write(json.dumps(line) + "\n")
    return directory


def _run_finishing_itself(home: Path, *, steps: Tuple[str, ...] = ("preflight",), **meta: Any):
    """The ``--posture-release`` shape: a run whose own lock is held by its finish.

    The lock keeps ``kind=dispatch`` and names no finish -- such a finish never adopts a
    finish lock -- which is why ``holders`` had nothing to draw for it.
    """
    _write_run(
        home, "run_a", task_id="task-001", project_id="alpha", mode="session", status="running"
    )
    _write_lock(home, "alpha~task-001", f"pid={os.getpid()} run=run_a kind={KIND_DISPATCH}")
    return _write_finish(home, "fin_own", "task-001", "alpha", steps=steps, run_id="run_a", **meta)


class TestFinishing:
    """A run whose task is being merged reads what the task reads (task-533)."""

    def test_a_run_finishing_itself_reads_finishing_not_working(self, two_projects):
        client, home, _, _ = two_projects
        _run_finishing_itself(home)

        row = _live(client)["runs"][0]
        assert row["health"] == "finishing"
        assert row["finish_id"] == "fin_own"
        assert row["finish_step"] == "runway"

    def test_the_task_read_says_finishing_about_the_same_task(self, two_projects):
        """The chip and the tile are one fact: assert both off the same machine."""
        client, home, _, _ = two_projects
        _run_finishing_itself(home)

        task = client.get("/api/projects/alpha/tasks/task-001/detail").json()["task"]
        row = {r["id"]: r for r in client.get("/api/projects/alpha/tasks").json()}["task-001"]
        assert row["display_status"] == "Landing"
        assert task["display_status"] == "Landing"
        assert _live(client)["runs"][0]["health"] == "finishing"

    def test_a_run_merely_working_still_reads_working(self, two_projects):
        client, home, _, _ = two_projects
        _write_run(
            home, "run_a", task_id="task-001", project_id="alpha", mode="session", status="running"
        )
        _write_lock(home, "alpha~task-001", f"pid={os.getpid()} run=run_a kind={KIND_DISPATCH}")

        row = _live(client)["runs"][0]
        assert row["health"] == "working"
        assert row["finish_id"] == ""

    @pytest.mark.parametrize("outcome", ["finished", "escalated", "declined"])
    def test_a_finish_that_ended_does_not_make_a_run_read_finishing(self, two_projects, outcome):
        client, home, _, _ = two_projects
        _run_finishing_itself(home, outcome=outcome, finished_at=_ago(5))
        assert _live(client)["runs"][0]["health"] == "working"

    def test_an_interrupted_finish_does_not_make_a_run_read_finishing(self, two_projects):
        """The lock is gone with its process, so the finish is interrupted, not live."""
        client, home, _, _ = two_projects
        _write_run(
            home, "run_a", task_id="task-001", project_id="alpha", mode="session", status="running"
        )
        _write_finish(home, "fin_dead", "task-001", "alpha")
        _write_lock(home, "alpha~task-001", f"pid=999999 run= kind={KIND_FINISH} finish=fin_dead")

        assert _live(client)["runs"][0]["health"] == "working"

    def test_finishing_outranks_a_parked_prompt_and_waiting_feedback(self, two_projects):
        """The ranking ``run_health`` states: a live finish is what is happening."""
        client, home, _, _ = two_projects
        _run_finishing_itself(home)
        meta_path = home / "runs" / "run_a" / "meta.yaml"
        meta = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        meta.update(status="parked", handback_pending=3)
        meta_path.write_text(yaml.safe_dump(meta), encoding="utf-8")

        assert _live(client)["runs"][0]["health"] == "finishing"

    def test_a_closed_task_reads_its_outcome_not_finishing(self, two_projects):
        """The task page says Completed while the finish cleans up; so does the tile."""
        client, home, alpha, _ = two_projects
        manager = TaskManager(task_store(alpha / "tasks"))
        manager.close_task("task-001", outcome=Outcome.COMPLETED, actor="claude")
        _run_finishing_itself(home, steps=("preflight", "runway", "rebase", "gate", "merge"))

        assert _live(client)["runs"][0]["health"] != "finishing"

    def test_a_queued_finish_with_no_lock_of_its_own_still_gets_a_card(self, two_projects):
        """a2: a live finish nothing else draws is drawn from its record."""
        client, home, _, _ = two_projects
        _write_finish(home, "fin_hand", "task-001", "alpha")
        # Held by a finish run by hand from a shell: a live process, no run record.
        _write_lock(home, "alpha~task-001", f"pid={os.getpid()} run= kind={KIND_DISPATCH}")

        body = _live(client)
        assert body["runs"] == []
        cards = [holder for holder in body["holders"] if holder["kind"] == KIND_FINISH]
        assert [(card["task_id"], card["finish_id"], card["detail"]) for card in cards] == [
            ("task-001", "fin_hand", "runway")
        ]
        assert cards[0]["task_url"] == "/p/alpha/tasks/task-001"

    def test_a_finish_the_run_tile_draws_gets_no_second_card(self, two_projects):
        client, home, _, _ = two_projects
        _run_finishing_itself(home)
        assert _live(client)["holders"] == []

    def test_the_runway_names_its_task_and_the_queue_names_the_runway(self, two_projects):
        """The task-526 report: a runway holder with an empty task id."""
        client, home, alpha, _ = two_projects
        _make_task(alpha, "task-002", "Hold the runway")
        _write_finish(home, "fin_front", "task-002", "alpha", steps=("preflight", "runway"))
        _write_lock(
            home,
            "alpha~task-002",
            f"pid={os.getpid()} run= kind={KIND_FINISH} finish=fin_front started={_ago(30)}",
        )
        _write_lock(
            home,
            runway_lock_name(alpha),
            f"pid={os.getpid()} run= kind={KIND_RUNWAY} finish=fin_front started={_ago(25)}",
        )
        _run_finishing_itself(home)

        body = _live(client)
        runway = next(holder for holder in body["holders"] if holder["kind"] == KIND_RUNWAY)
        assert runway["task_id"] == "task-002"
        assert runway["task_title"] == "Hold the runway"
        run = body["runs"][0]
        assert (run["health"], run["finish_step"], run["runway_behind"]) == (
            "finishing",
            "runway",
            "task-002",
        )


class TestEpicWalks:
    """Open epic walks, on the one payload the dashboard already polls (task-523).

    A walk is the only thing on this machine that dispatches work with no human act at
    the moment of dispatch, and since task-458 it is hosted by the server rather than by
    a blocking process -- so nothing about it reached any surface and its children
    arrived unexplained. These assert the counts and the grounded state a reader acts
    on, which is what the section exists to say.
    """

    def _epic(self, root: Path, *, children: int = 3, completed: int = 0) -> TaskManager:
        """One parent with ``children`` children, the first ``completed`` of them closed."""
        manager = TaskManager(task_store(root / "tasks"))
        manager.create_task(
            id="task-900",
            title="Walk the epic",
            summary="An epic with children.",
            description="An epic with children, at length.",
            actor="claude",
        )
        for index in range(children):
            child_id = f"task-9{index + 10}"
            manager.create_task(
                id=child_id,
                title=f"Child {index}",
                summary=f"Child {index}.",
                description=f"Child {index}, at length.",
                actor="claude",
                lifecycle=Lifecycle.READY,
                parent="task-900",
            )
            if index < completed:
                manager.close_task(child_id, actor="claude", outcome=Outcome.COMPLETED)
        return manager

    def _walk(self, home: Path, project_id: str = "alpha", parent: str = "task-900"):
        walk, _ = journal(home).open_walk(
            project_id=project_id,
            parent_task_id=parent,
            authority_entry=3,
            authority_actor="Jeff Posey",
            settings={},
            host="server",
        )
        return walk

    def _fly(self, home: Path, walk, child_id: str) -> None:
        store = journal(home)
        store.reserve_child_attempt(
            walk.walk_id,
            epoch=walk.epoch,
            child_task_id=child_id,
            operation_id=f"op-{child_id}",
            limit=2,
            used_on_record=0,
        )
        store.record_child(walk.walk_id, epoch=walk.epoch, child_task_id=child_id, status="flying")

    def test_names_the_epic_and_counts_its_children(self, two_projects):
        """ac-1. The counts come from the task graph, not from the walk's own rows."""
        client, home, alpha, _ = two_projects
        self._epic(alpha, children=4, completed=1)
        walk = self._walk(home)
        self._fly(home, walk, "task-911")

        [row] = _live(client)["walks"]
        assert row["parent_task_id"] == "task-900"
        assert row["parent_task_title"] == "Walk the epic"
        assert row["parent_task_url"] == "/p/alpha/tasks/task-900"
        assert row["project_name"] == "Alpha Project"
        assert row["children_total"] == 4
        assert row["children_completed"] == 1
        assert row["children_in_flight"] == 1
        assert row["children_remaining"] == 2
        assert row["in_flight_task_ids"] == ["task-911"]
        assert row["grounded"] is False

    def test_counts_children_the_walk_has_not_touched_yet(self, two_projects):
        """The walk has a row only for a child it has flown, so its rows are not the total.

        A five-child epic that has flown two would otherwise report three children, and
        a reader would think the epic was nearly done when it had barely started.
        """
        client, home, alpha, _ = two_projects
        self._epic(alpha, children=5)
        self._walk(home)

        [row] = _live(client)["walks"]
        assert row["children_total"] == 5
        assert row["children_in_flight"] == 0
        assert row["children_remaining"] == 5

    def test_a_grounded_walk_says_so_and_says_why(self, two_projects):
        """ac-1. The field the section exists for: a stall must not read as progress."""
        client, home, alpha, _ = two_projects
        self._epic(alpha, children=2)
        walk = self._walk(home)
        journal(home).update_walk(
            walk.walk_id,
            epoch=walk.epoch,
            grounding={
                "stop": "child_exhausted_attempts",
                "detail": "task-910 used both attempts",
                "child": "task-910",
            },
            detail="task-910 used both attempts",
        )

        [row] = _live(client)["walks"]
        assert row["grounded"] is True
        assert row["grounded_reason"] == "child_exhausted_attempts"
        assert row["grounded_word"] == "a child used both of its attempts"
        # A real stop: nothing clears it but a person.
        assert row["resumes_by_itself"] is False
        assert row["waiting_on_task_id"] == ""
        assert row["detail"] == "task-910 used both attempts"

    def test_a_walk_waiting_on_a_person_names_the_child_and_says_it_resumes(self, two_projects):
        """task-467's distinction, on the payload. Both are grounded; only one resumes."""
        client, home, alpha, _ = two_projects
        manager = self._epic(alpha, children=2)
        manager.claim_task("task-910", agent="claude")
        manager.handoff(
            "task-910",
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Please review the branch.",
        )
        walk = self._walk(home)
        journal(home).update_walk(
            walk.walk_id,
            epoch=walk.epoch,
            grounding={
                "stop": "child_needs_a_human",
                "detail": "task-910 is with a person",
                "child": "task-910",
            },
        )

        [row] = _live(client)["walks"]
        assert row["grounded"] is True
        assert row["grounded_word"] == "a child needs a person"
        assert row["resumes_by_itself"] is True
        assert row["waiting_on_task_id"] == "task-910"
        assert row["waiting_on_task_title"] == "Child 0"
        assert row["waiting_on_task_url"] == "/p/alpha/tasks/task-910"

    def test_a_walk_takes_no_slot(self, two_projects):
        """ac-2, on the server side of it. A walk is supervision, not a run slot."""
        client, home, alpha, _ = two_projects
        self._epic(alpha, children=2)
        walk = self._walk(home)
        self._fly(home, walk, "task-910")

        body = _live(client)
        assert body["walks"]
        assert body["occupied"] == 0
        assert body["runs"] == []

    def test_no_open_walk_sends_an_empty_list(self, two_projects):
        """ac-3's server half: nothing to render means nothing to send."""
        client, _, _, _ = two_projects
        assert _live(client)["walks"] == []

    def test_a_finished_walk_is_not_sent(self, two_projects):
        client, home, alpha, _ = two_projects
        self._epic(alpha, children=1)
        walk = self._walk(home)
        journal(home).update_walk(walk.walk_id, epoch=walk.epoch, state="done")

        assert _live(client)["walks"] == []

    def test_returns_walks_from_every_project_with_their_own_names(self, two_projects):
        """The reason this rides on the machine-wide payload: the epic walking on the
        other project is still starting children on this machine's slots."""
        client, home, alpha, beta = two_projects
        self._epic(alpha, children=1)
        self._epic(beta, children=1)
        self._walk(home, project_id="alpha")
        self._walk(home, project_id="beta")

        rows = {row["project_id"]: row for row in _live(client)["walks"]}
        assert set(rows) == {"alpha", "beta"}
        assert rows["beta"]["project_name"] == "Beta Project"
        assert rows["beta"]["parent_task_url"] == "/p/beta/tasks/task-900"

    def test_a_walk_whose_backlog_cannot_be_read_still_appears(self, two_projects):
        """A status page shows the row it cannot fill in rather than dropping it.

        A walk on a parent that is not in the backlog at all -- unregistered, renamed,
        mid-migration -- is exactly the thing worth seeing, and zero counts beside a
        named parent are readable where a missing row is not.
        """
        client, home, alpha, _ = two_projects
        self._epic(alpha, children=1)
        self._walk(home, parent="task-nope")

        [row] = _live(client)["walks"]
        assert row["parent_task_id"] == "task-nope"
        assert row["children_total"] == 0
        assert row["parent_task_title"] == ""


class TestAWalkedEpicReadsWalking:
    """A walked epic's own status chip says "Walking", on every task read (task-591).

    The epic is claimed and reads agent/work while its walk is open, so without the walk
    folded into ``task_status`` every surface called it "Working". Asserted on the list,
    the record and the dashboard card: three read models, one derivation.
    """

    def _claimed_epic(self, alpha: Path) -> TaskManager:
        """A ready epic with one open child, claimed as the supervisor's seat."""
        manager = TaskManager(task_store(alpha / "tasks"))
        manager.create_task(
            id="task-900",
            title="Walk the epic",
            summary="An epic with children.",
            description="An epic with children, at length.",
            actor="claude",
            lifecycle=Lifecycle.READY,
        )
        manager.create_task(
            id="task-910",
            title="Child",
            summary="Child.",
            description="Child, at length.",
            actor="claude",
            lifecycle=Lifecycle.READY,
            parent="task-900",
        )
        manager.claim_task("task-900", agent="claude")
        return manager

    def _row(self, client: TestClient, path: str) -> Dict[str, Any]:
        response = client.get(path)
        assert response.status_code == 200, response.text
        parsed: Dict[str, Any] = response.json()
        return parsed

    def _walk(self, home: Path, parent: str = "task-900") -> Any:
        return TestEpicWalks._walk(self, home, parent=parent)  # type: ignore[arg-type]

    def _listed(self, client: TestClient) -> Dict[str, Any]:
        response = client.get("/api/projects/alpha/tasks")
        assert response.status_code == 200, response.text
        rows: List[Dict[str, Any]] = response.json()
        return next(row for row in rows if row["id"] == "task-900")

    def test_an_open_walk_turns_working_into_walking_on_every_read(self, two_projects):
        client, home, alpha, _ = two_projects
        self._claimed_epic(alpha)
        assert self._listed(client)["display_status"] == "Working"

        walk = self._walk(home)

        listed = self._listed(client)
        # The task page's read. The bare `GET /tasks/{id}` is the record alone and
        # carries no derived fact, a live finish's included.
        record = self._row(client, "/api/projects/alpha/tasks/task-900/detail")["task"]
        for row in (listed, record):
            assert row["display_status"] == "Walking"
            assert row["status_category"] == "working"
            assert row["live_walk"] == {"walk_id": walk.walk_id, "grounded": False}

    def test_a_grounded_walk_still_reads_walking_and_says_it_is_grounded(self, two_projects):
        client, home, alpha, _ = two_projects
        self._claimed_epic(alpha)
        walk = self._walk(home)
        journal(home).update_walk(
            walk.walk_id,
            epoch=walk.epoch,
            grounding={"stop": "child_needs_a_human", "detail": "", "child": "task-910"},
            detail="",
        )

        listed = self._listed(client)
        assert listed["display_status"] == "Walking"
        assert listed["live_walk"]["grounded"] is True

    def test_another_tasks_walk_changes_nothing_here(self, two_projects):
        client, home, alpha, _ = two_projects
        self._claimed_epic(alpha)
        self._walk(home, parent="task-001")

        listed = self._listed(client)
        assert listed["display_status"] == "Working"
        assert listed["live_walk"] is None

    def test_a_walked_epic_handed_to_a_person_says_what_the_person_does(self, two_projects):
        """Walking replaces Working and nothing else."""
        client, home, alpha, _ = two_projects
        manager = self._claimed_epic(alpha)
        manager.handoff(
            "task-900",
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Look at it.",
        )
        self._walk(home)

        assert self._listed(client)["display_status"] == "Needs review"
