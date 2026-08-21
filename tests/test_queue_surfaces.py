"""The queue over HTTP, through the Python client, and on the command line.

Task-206, implementing the REST, Python-client and CLI rows of section 10 of
``docs/task-selection-design.md``. Task-205 gave the manager the verbs; nothing outside
the process could reach them. These are the tests for the reaching.

The one to read first is :class:`TestCorruptionReachesEverySurface`. Design section 8
says a broken queue is refused loudly by whatever answers "what next" and rendered
patiently by whatever exists to fix it, and a rule stated in two halves like that is
exactly the kind that gets implemented in one place and forgotten in the other.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agentjobs.api.dependencies import get_task_manager, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.api.models import TaskRead, TaskUpdateRequest
from agentjobs.api.routes.status import get_acting_project
from agentjobs.cli import app as cli_app
from agentjobs.client import TaskClient
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Lifecycle, LogEntryType, Outcome, Priority
from agentjobs.projects import Project
from agentjobs.queue import REPAIR_COMMAND
from agentjobs.storage import TaskStorage

runner = CliRunner()

CONFIG: Dict[str, object] = {
    "project_name": "Fixture",
    "tasks_directory": "tasks",
    "categories": ["general"],
    "actors": [
        {"name": "Ada", "kind": "human", "display_name": "Ada Lovelace"},
        {"name": "bot", "kind": "agent", "display_name": "Bot"},
    ],
    "default_user": "Ada",
}

#: Every mutating route this task adds, with a body that would otherwise succeed.
#: Kept as data so sc-1's "refuses a request with no actor or no operation_id" is one
#: assertion over the whole surface rather than four nearly identical tests -- a shape
#: that also fails loudly when somebody adds a fifth route and forgets to list it.
MUTATING_ROUTES: List[Tuple[str, Dict[str, Any]]] = [
    ("/api/tasks/task-a/queue-move", {"top": True}),
    ("/api/tasks/task-a/reprioritize", {"priority": "low"}),
    ("/api/queue/repair", {}),
    ("/api/queue/compact", {"band": "high"}),
]


@pytest.fixture()
def project(tmp_path: Path) -> Iterator[Tuple[Path, TaskManager]]:
    """A project directory with config, an empty tasks directory, and its manager."""
    (tmp_path / ".agentjobs").mkdir(parents=True)
    (tmp_path / ".agentjobs" / "config.yaml").write_text(yaml.safe_dump(CONFIG), encoding="utf-8")
    yield tmp_path, TaskManager(TaskStorage(tmp_path / "tasks"))


@pytest.fixture()
def api(project) -> Iterator[Tuple[TestClient, TaskManager, Path]]:
    """A TestClient bound to the fixture project, acting as that project.

    The acting project is overridden alongside the manager because actor validation
    resolves the *default* project otherwise -- which, with an empty registry, is the
    working directory: the AgentJobs repository itself. These tests would then be
    checking their actor names against the real ``.agentjobs/config.yaml``.
    """
    root, manager = project
    reset_dependency_cache()
    acting = Project(id="fixture", name="Fixture", root=root)
    app.dependency_overrides[get_task_manager] = lambda: manager
    app.dependency_overrides[get_acting_project] = lambda: acting
    with TestClient(app) as client:
        yield client, manager, root
    app.dependency_overrides.clear()
    reset_dependency_cache()


def make(
    manager: TaskManager,
    task_id: str,
    *,
    priority: Priority = Priority.HIGH,
    lifecycle: Lifecycle = Lifecycle.READY,
    title: str = "",
    **kwargs: Any,
) -> str:
    """Create one task through the real verb, so it gets a real position."""
    manager.create_task(
        id=task_id,
        title=title or f"Title of {task_id}",
        description="Body.",
        priority=priority,
        lifecycle=lifecycle,
        actor="bot",
        **kwargs,
    )
    return task_id


def revision(manager: TaskManager, task_id: str) -> str:
    """The ``updated`` stamp a caller would send back as ``expected_revision``."""
    task = manager.get_task(task_id)
    assert task is not None
    return task.updated.isoformat()


def position(manager: TaskManager, task_id: str) -> int:
    task = manager.get_task(task_id)
    assert task is not None
    assert task.queue_position is not None
    return task.queue_position


def queue_moves(manager: TaskManager, task_id: str) -> List[Any]:
    """The ``queue_move`` entries on one task -- the record of every decision made."""
    task = manager.get_task(task_id)
    assert task is not None
    return [entry for entry in task.log if entry.type is LogEntryType.QUEUE_MOVE]


def order(manager: TaskManager, priority: Priority = Priority.HIGH) -> List[str]:
    """The band as it stands on disk, in queue order."""
    return [
        task.id
        for task in sorted(
            (
                task
                for task in manager.storage.list_tasks_uncached()
                if task.is_open and task.priority is priority
            ),
            key=lambda task: (task.queue_position or 0, task.id),
        )
    ]


def break_the_queue(root: Path, task_id: str, *, band: str, at: int) -> None:
    """Force a duplicate position by hand, as a bad merge or a stray editor would.

    Deliberately raw. Every verb in the system refuses to produce this state, which is
    the point -- the corruption these surfaces have to survive arrives from outside
    them, so a test that produced it through a verb would be testing nothing.
    """
    path = root / "tasks" / f"{task_id}.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["queue_position"] = at
    raw["priority"] = band
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# sc-2 -- the rule stated in two halves
# ---------------------------------------------------------------------------


class TestCorruptionReachesEverySurface:
    """A broken queue is refused by what answers, and rendered by what repairs.

    Design section 8 is one rule with two obligations, and they pull in opposite
    directions: selection must refuse rather than guess, while `check`, `repair` and
    `list` must keep working *because* it is broken. Implementing one and forgetting
    the other is the easy mistake, and it is silent in both directions -- a `list` that
    raises leaves you with no way to see the damage, and a `next` that answers hands
    somebody the wrong task with no trace at all.
    """

    def test_next_answers_409_naming_the_offenders_and_the_repair(self, api) -> None:
        client, manager, root = api
        first = make(manager, "task-a")
        second = make(manager, "task-b")
        break_the_queue(root, second, band="high", at=position(manager, first))

        response = client.get("/api/tasks/next")

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert first in detail and second in detail
        assert REPAIR_COMMAND in detail

    def test_explain_answers_409_for_the_same_reason(self, api) -> None:
        """The explanation asserts an order over the skipped tasks, so it is no safer."""
        client, manager, root = api
        first = make(manager, "task-a")
        second = make(manager, "task-b")
        break_the_queue(root, second, band="high", at=position(manager, first))

        response = client.get("/api/tasks/next/explain")

        assert response.status_code == 409
        assert REPAIR_COMMAND in response.json()["detail"]

    def test_queue_listing_renders_the_broken_band_and_names_the_problem(self, api) -> None:
        client, manager, root = api
        first = make(manager, "task-a")
        second = make(manager, "task-b")
        break_the_queue(root, second, band="high", at=position(manager, first))

        response = client.get("/api/queue")

        assert response.status_code == 200
        body = response.json()
        assert body["repair_command"] == REPAIR_COMMAND
        assert [problem["kind"] for problem in body["problems"]] == ["duplicate"]
        assert sorted(body["problems"][0]["tasks"]) == sorted([first, second])
        listed = {entry["task"] for band in body["bands"] for entry in band["entries"]}
        assert {first, second} <= listed

    def test_repair_fixes_it_and_selection_answers_again(self, api) -> None:
        client, manager, root = api
        first = make(manager, "task-a")
        second = make(manager, "task-b")
        break_the_queue(root, second, band="high", at=position(manager, first))

        repaired = client.post(
            "/api/queue/repair", json={"actor": "Ada", "operation_id": "op-repair"}
        )

        assert repaired.status_code == 200
        body = repaired.json()
        assert body["changed"] is True
        # Everything a repair guessed is named, because the tie-break is arbitrary by
        # necessity and naming it is what makes the guess reviewable.
        assert [item["task"] for item in body["assigned"]] == [second]
        assert client.get("/api/tasks/next").status_code == 200

    def test_cli_next_exits_non_zero_with_the_same_message(self, project, monkeypatch) -> None:
        root, manager = project
        first = make(manager, "task-a")
        second = make(manager, "task-b")
        break_the_queue(root, second, band="high", at=position(manager, first))
        monkeypatch.chdir(root)

        result = runner.invoke(cli_app, ["next"])

        assert result.exit_code == 1
        assert REPAIR_COMMAND in result.output
        assert first in result.output and second in result.output

    def test_cli_check_and_list_still_work_against_it(self, project, monkeypatch) -> None:
        """`check` and `list` report rather than raise: you must be able to see it."""
        root, manager = project
        first = make(manager, "task-a")
        second = make(manager, "task-b")
        break_the_queue(root, second, band="high", at=position(manager, first))
        monkeypatch.chdir(root)

        checked = runner.invoke(cli_app, ["queue", "check"])
        listed = runner.invoke(cli_app, ["queue", "list"])

        assert checked.exit_code == 0
        assert REPAIR_COMMAND in checked.output
        assert listed.exit_code == 0
        assert first in listed.output and second in listed.output

    def test_cli_check_strict_is_the_form_a_script_uses(self, project, monkeypatch) -> None:
        root, manager = project
        first = make(manager, "task-a")
        second = make(manager, "task-b")
        break_the_queue(root, second, band="high", at=position(manager, first))
        monkeypatch.chdir(root)

        assert runner.invoke(cli_app, ["queue", "check", "--strict"]).exit_code == 1

    def test_cli_repair_reports_what_it_guessed(self, project, monkeypatch) -> None:
        root, manager = project
        first = make(manager, "task-a")
        second = make(manager, "task-b")
        break_the_queue(root, second, band="high", at=position(manager, first))
        monkeypatch.chdir(root)

        result = runner.invoke(cli_app, ["queue", "repair"])

        assert result.exit_code == 0
        assert second in result.output
        assert "guessed" in result.output
        assert runner.invoke(cli_app, ["queue", "check"]).exit_code == 0


# ---------------------------------------------------------------------------
# sc-1 -- every verb exists, and none of them is anonymous
# ---------------------------------------------------------------------------


class TestEveryMutatingRouteIsAttributedAndRetrySafe:
    """No queue mutation is anonymous, and none can be applied twice by a retry.

    ``operation_id`` is optional on the older verbs so callers written before it
    existed keep working. It is required here because nothing was ever written against
    these routes, and a reorder a timeout can silently repeat puts a task somewhere
    nobody asked for while leaving two entries each claiming to be the decision.
    """

    @pytest.mark.parametrize("path,extra", MUTATING_ROUTES)
    def test_refused_without_an_actor(self, api, path: str, extra: Dict[str, Any]) -> None:
        client, manager, _ = api
        make(manager, "task-a")
        response = client.post(path, json={"operation_id": "op-1", **extra})
        assert response.status_code == 400

    @pytest.mark.parametrize("path,extra", MUTATING_ROUTES)
    def test_refused_without_an_operation_id(self, api, path: str, extra: Dict[str, Any]) -> None:
        client, manager, _ = api
        make(manager, "task-a")
        response = client.post(path, json={"actor": "Ada", **extra})
        assert response.status_code == 400

    @pytest.mark.parametrize("path,extra", MUTATING_ROUTES)
    def test_refused_with_an_actor_the_project_does_not_define(
        self, api, path: str, extra: Dict[str, Any]
    ) -> None:
        """An attribution nobody can resolve is worse than a refused request (D2)."""
        client, manager, _ = api
        make(manager, "task-a")
        response = client.post(path, json={"actor": "nobody", "operation_id": "op-1", **extra})
        assert response.status_code == 400
        assert response.json()["code"] == "unknown_actor"

    def test_a_replayed_move_does_not_move_the_task_twice(self, api) -> None:
        client, manager, _ = api
        first = make(manager, "task-a")
        make(manager, "task-b")
        make(manager, "task-c")
        payload = {"actor": "Ada", "operation_id": "op-move", "bottom": True}
        stamp = revision(manager, first)

        client.post(f"/api/tasks/{first}/queue-move", json={**payload, "expected_revision": stamp})
        landed = position(manager, first)
        replayed = client.post(
            f"/api/tasks/{first}/queue-move",
            json={**payload, "expected_revision": stamp},
            params={"envelope": "true"},
        )

        assert replayed.status_code == 200
        assert replayed.json()["replayed"] is True
        assert position(manager, first) == landed
        assert len(queue_moves(manager, first)) == 1


class TestQueueMoveOverHttp:
    """The route names a placement; the manager decides what number that is."""

    def test_move_to_top_reorders_the_band(self, api) -> None:
        client, manager, _ = api
        first, second, third = (make(manager, name) for name in ("task-a", "task-b", "task-c"))
        assert order(manager) == [first, second, third]

        response = client.post(
            f"/api/tasks/{third}/queue-move",
            json={"actor": "Ada", "operation_id": "op-1", "top": True},
        )

        assert response.status_code == 200
        assert order(manager) == [third, first, second]

    def test_move_before_a_named_neighbour(self, api) -> None:
        client, manager, _ = api
        first, second, third = (make(manager, name) for name in ("task-a", "task-b", "task-c"))

        response = client.post(
            f"/api/tasks/{third}/queue-move",
            json={"actor": "Ada", "operation_id": "op-1", "before": second},
        )

        assert response.status_code == 200
        assert order(manager) == [first, third, second]

    def test_two_placements_are_refused_before_anything_is_written(self, api) -> None:
        """ "Before task-b and also at the top" is two answers to one question."""
        client, manager, _ = api
        first = make(manager, "task-a")
        second = make(manager, "task-b")
        before = position(manager, second)

        response = client.post(
            f"/api/tasks/{second}/queue-move",
            json={"actor": "Ada", "operation_id": "op-1", "top": True, "before": first},
        )

        assert response.status_code == 400
        assert position(manager, second) == before

    def test_no_placement_at_all_is_refused(self, api) -> None:
        client, manager, _ = api
        make(manager, "task-a")
        response = client.post(
            "/api/tasks/task-a/queue-move", json={"actor": "Ada", "operation_id": "op-1"}
        )
        assert response.status_code == 400

    def test_with_children_carries_the_open_same_band_descendants(self, api) -> None:
        client, manager, _ = api
        parent = make(manager, "task-a")
        child = make(manager, "task-b", parent=parent)
        other = make(manager, "task-c")
        # Put the pair behind `other` so a group move to the top is observable.
        client.post(
            f"/api/tasks/{other}/queue-move",
            json={"actor": "Ada", "operation_id": "op-0", "top": True},
        )

        response = client.post(
            f"/api/tasks/{parent}/queue-move",
            json={
                "actor": "Ada",
                "operation_id": "op-1",
                "top": True,
                "with_children": True,
            },
        )

        assert response.status_code == 200
        assert order(manager) == [parent, child, other]

    def test_a_stale_expected_revision_is_refused(self, api) -> None:
        client, manager, _ = api
        first = make(manager, "task-a")
        make(manager, "task-b")
        stale = revision(manager, first)
        client.post(
            f"/api/tasks/{first}/queue-move",
            json={"actor": "Ada", "operation_id": "op-1", "bottom": True},
        )

        response = client.post(
            f"/api/tasks/{first}/queue-move",
            json={
                "actor": "Ada",
                "operation_id": "op-2",
                "top": True,
                "expected_revision": stale,
            },
        )

        assert response.status_code == 409
        assert response.json()["code"] == "revision_conflict"

    def test_a_missing_task_is_404_not_409(self, api) -> None:
        client, _, _ = api
        response = client.post(
            "/api/tasks/task-nope/queue-move",
            json={"actor": "Ada", "operation_id": "op-1", "top": True},
        )
        assert response.status_code == 404
        assert response.json()["code"] == "task_not_found"


class TestReprioritizeOverHttp:
    """Band and place in one decision, with the band change recorded on the entry."""

    def test_moves_the_task_between_bands_at_the_bottom_by_default(self, api) -> None:
        client, manager, _ = api
        first = make(manager, "task-a", priority=Priority.LOW)
        make(manager, "task-b", priority=Priority.HIGH)

        response = client.post(
            f"/api/tasks/{first}/reprioritize",
            json={"actor": "Ada", "operation_id": "op-1", "priority": "high"},
        )

        assert response.status_code == 200
        assert order(manager, Priority.HIGH) == ["task-b", first]
        assert order(manager, Priority.LOW) == []

    def test_an_explicit_top_placement_is_honoured(self, api) -> None:
        client, manager, _ = api
        first = make(manager, "task-a", priority=Priority.LOW)
        make(manager, "task-b", priority=Priority.HIGH)

        client.post(
            f"/api/tasks/{first}/reprioritize",
            json={"actor": "Ada", "operation_id": "op-1", "priority": "high", "top": True},
        )

        assert order(manager, Priority.HIGH) == [first, "task-b"]

    def test_the_entry_records_the_band_it_came_from(self, api) -> None:
        client, manager, _ = api
        first = make(manager, "task-a", priority=Priority.LOW)

        client.post(
            f"/api/tasks/{first}/reprioritize",
            json={"actor": "Ada", "operation_id": "op-1", "priority": "critical"},
        )

        task = manager.get_task(first)
        assert task is not None
        entry = [item for item in task.log if item.type is LogEntryType.QUEUE_MOVE][-1]
        assert entry.data["from_band"] == "low"
        assert entry.data["band"] == "critical"

    def test_two_placements_are_refused(self, api) -> None:
        client, manager, _ = api
        make(manager, "task-a")
        make(manager, "task-b")
        response = client.post(
            "/api/tasks/task-b/reprioritize",
            json={
                "actor": "Ada",
                "operation_id": "op-1",
                "priority": "high",
                "top": True,
                "before": "task-a",
            },
        )
        assert response.status_code == 400


class TestQueueListingOverHttp:
    """The list a human reviews: every band, every open task, and why each is skipped."""

    def test_lists_every_band_including_the_empty_ones(self, api) -> None:
        client, manager, _ = api
        make(manager, "task-a", priority=Priority.HIGH)

        body = client.get("/api/queue").json()

        assert [band["band"] for band in body["bands"]] == ["critical", "high", "medium", "low"]
        assert body["problems"] == []

    def test_each_entry_carries_its_claimability_and_the_reason_it_is_not(self, api) -> None:
        client, manager, _ = api
        parent = make(manager, "task-a")
        make(manager, "task-b", parent=parent)
        make(manager, "task-c", lifecycle=Lifecycle.DRAFT)

        entries = {
            entry["task"]: entry
            for band in client.get("/api/queue").json()["bands"]
            for entry in band["entries"]
        }

        assert entries[parent]["claimable"] is False
        assert entries[parent]["reason"] == "has 1 open child"
        assert entries["task-c"]["claimable"] is False
        assert "draft" in entries["task-c"]["reason"]
        assert entries["task-b"]["claimable"] is True
        assert entries["task-b"]["reason"] is None

    def test_the_reason_matches_what_explain_says_about_the_same_task(self, api) -> None:
        """One sentence, one source. A listing and an explanation may not disagree."""
        client, manager, _ = api
        parent = make(manager, "task-a")
        make(manager, "task-b", parent=parent)

        listed = {
            entry["task"]: entry["reason"]
            for band in client.get("/api/queue").json()["bands"]
            for entry in band["entries"]
        }
        explained = {
            item["task"]: item["reason"]
            for item in client.get("/api/tasks/next/explain").json()["skipped"]
        }

        assert explained
        assert all(listed[task] == reason for task, reason in explained.items())

    def test_an_agent_filter_reports_eligibility_as_the_reason(self, api) -> None:
        client, manager, _ = api
        make(manager, "task-a", assignment={"eligible": ["codex"]})

        entries = {
            entry["task"]: entry
            for band in client.get("/api/queue", params={"agent": "bot"}).json()["bands"]
            for entry in band["entries"]
        }

        assert entries["task-a"]["claimable"] is False
        assert entries["task-a"]["reason"] == "restricted to codex"

    def test_closed_tasks_are_not_in_line(self, api) -> None:
        client, manager, _ = api
        make(manager, "task-a")
        manager.close_task("task-a", actor="bot", outcome=Outcome.COMPLETED)

        listed = {
            entry["task"]
            for band in client.get("/api/queue").json()["bands"]
            for entry in band["entries"]
        }

        assert listed == set()

    def test_the_scoped_and_unscoped_mounts_are_the_same_handler(self, api) -> None:
        """Mounted twice on purpose; a second implementation would be free to drift."""
        client, manager, _ = api
        make(manager, "task-a")
        assert client.get("/api/queue").json() == client.get("/api/projects/fixture/queue").json()


class TestExplainNextOverHttp:
    """Design section 9's structure, transcribed rather than re-derived."""

    def test_names_the_winner_its_band_and_its_position(self, api) -> None:
        client, manager, _ = api
        make(manager, "task-a", priority=Priority.MEDIUM)

        body = client.get("/api/tasks/next/explain").json()

        assert body["task"] == "task-a"
        assert body["band"] == "medium"
        assert body["queue_position"] == position(manager, "task-a")
        assert body["empty_bands_above"] == ["critical", "high"]

    def test_lists_what_was_skipped_ahead_of_it_with_the_rule_that_did_it(self, api) -> None:
        client, manager, _ = api
        parent = make(manager, "task-a")
        make(manager, "task-b", parent=parent)
        make(manager, "task-c")

        body = client.get("/api/tasks/next/explain").json()

        assert body["task"] == "task-b"
        assert [item["task"] for item in body["skipped"]] == [parent]
        assert body["skipped"][0]["reason"] == "has 1 open child"

    def test_with_nothing_claimable_every_open_task_is_explained(self, api) -> None:
        """The listing a reader wants precisely when told there is nothing to do."""
        client, manager, _ = api
        make(manager, "task-a", lifecycle=Lifecycle.DRAFT)

        body = client.get("/api/tasks/next/explain").json()

        assert body["task"] is None
        assert [item["task"] for item in body["skipped"]] == ["task-a"]

    def test_next_is_not_captured_as_a_task_id(self, api) -> None:
        """`/tasks/next/explain` must not resolve through `/tasks/{task_id}`."""
        client, manager, _ = api
        make(manager, "task-a")
        assert client.get("/api/tasks/next/explain").status_code == 200


class TestQueueMaintenanceOverHttp:
    """Repair and compaction: idempotent by construction, and never automatic."""

    def test_compact_renumbers_a_band_without_changing_anyone_s_place(self, api) -> None:
        client, manager, _ = api
        first, second, third = (make(manager, name) for name in ("task-a", "task-b", "task-c"))
        client.post(
            f"/api/tasks/{third}/queue-move",
            json={"actor": "Ada", "operation_id": "op-1", "top": True},
        )
        before = order(manager)

        response = client.post(
            "/api/queue/compact",
            json={"actor": "Ada", "operation_id": "op-2", "band": "high"},
        )

        assert response.status_code == 200
        assert response.json()["band"] == "high"
        assert order(manager) == before
        assert [position(manager, task_id) for task_id in before] == [100, 200, 300]

    def test_compact_reports_where_each_task_landed_exactly_once(self, api) -> None:
        """A renumber writes some tasks twice; only the last write is where they are.

        Found on a live 218-task corpus rather than by a test, which is why this one
        exists. ``plan_renumber`` uses up to two passes -- tail-first upward,
        head-first downward -- so no intermediate state ever holds two tasks on one
        number. A task moved by both passes is therefore written twice, and the
        manager returns both writes because both happened. Reporting them straight
        through made a compaction claim it had put one task in two places, which is
        precisely the thing a compaction never does.
        """
        client, manager, _ = api
        for name in ("task-a", "task-b", "task-c", "task-d"):
            make(manager, name)
        # Close the third, leaving 100, 200, 400 against targets 100, 200, 300. The
        # ranges overlap, which is what forces the two-pass form -- and it is also
        # the ordinary shape of a band that has had work closed out of the middle of
        # it, which is to say the shape every real compaction meets.
        manager.close_task("task-c", actor="bot", outcome=Outcome.COMPLETED)

        moved = client.post(
            "/api/queue/compact",
            json={"actor": "Ada", "operation_id": "op-1", "band": "high"},
        ).json()["moved"]

        reported = [item["task"] for item in moved]
        assert len(reported) == len(set(reported))
        # And what it reports is the truth on disk, in the order a reader expects.
        assert [item["position"] for item in moved] == sorted(item["position"] for item in moved)
        assert {item["task"]: item["position"] for item in moved} == {
            task_id: position(manager, task_id) for task_id in order(manager)
        }

    def test_compacting_twice_leaves_the_same_corpus(self, api) -> None:
        """Why these two need no operation ledger: repeating them changes nothing."""
        client, manager, _ = api
        for name in ("task-a", "task-b"):
            make(manager, name)
        payload = {"actor": "Ada", "operation_id": "op-1", "band": "high"}

        client.post("/api/queue/compact", json=payload)
        once = {task_id: position(manager, task_id) for task_id in order(manager)}
        client.post("/api/queue/compact", json={**payload, "operation_id": "op-2"})

        assert {task_id: position(manager, task_id) for task_id in order(manager)} == once

    def test_repairing_a_sound_queue_changes_nothing(self, api) -> None:
        client, manager, _ = api
        make(manager, "task-a")
        before = position(manager, "task-a")

        body = client.post(
            "/api/queue/repair", json={"actor": "Ada", "operation_id": "op-1"}
        ).json()

        assert body["changed"] is False
        assert body["assigned"] == []
        assert position(manager, "task-a") == before


# ---------------------------------------------------------------------------
# sc-4 -- the allowlist is the guard
# ---------------------------------------------------------------------------


class TestQueuePositionIsUnreachableByGenericPatch:
    """`queue_position` may be read everywhere and written through nothing generic.

    ``TaskUpdateRequest`` is an allowlist, and this is the field it exists to keep out:
    a patch that could set a number would be choosing a place without knowing what else
    is in the band, which is precisely how two tasks come to share one. The verbs take
    a *placement* and compute the number under the queue lock.
    """

    def test_the_update_model_has_no_queue_position_field(self) -> None:
        assert "queue_position" not in TaskUpdateRequest.model_fields

    def test_the_update_model_rejects_it_outright(self) -> None:
        with pytest.raises(ValueError):
            TaskUpdateRequest(queue_position=50)  # type: ignore[call-arg]

    def test_a_patch_carrying_it_is_refused_and_writes_nothing(self, api) -> None:
        client, manager, _ = api
        make(manager, "task-a")
        before = position(manager, "task-a")

        response = client.patch("/api/tasks/task-a", json={"queue_position": 50})

        assert response.status_code == 400
        assert position(manager, "task-a") == before

    def test_but_it_is_readable_on_every_read_model(self, api) -> None:
        """Unreachable by patch, never invisible: the surfaces have to render it."""
        client, manager, _ = api
        make(manager, "task-a")
        expected = position(manager, "task-a")

        assert "queue_position" in TaskRead.model_fields
        assert client.get("/api/tasks/task-a").json()["queue_position"] == expected
        assert client.get("/api/tasks").json()[0]["queue_position"] == expected
        assert client.get("/api/tasks/next").json()["queue_position"] == expected
        assert client.get("/api/tasks/task-a/detail").json()["task"]["queue_position"] == expected


# ---------------------------------------------------------------------------
# The Python client
# ---------------------------------------------------------------------------


@pytest.fixture()
def client_for(api) -> Iterator[Tuple[TaskClient, TaskManager]]:
    """A TaskClient talking to the TestClient's app over its ASGI transport."""
    test_client, manager, _ = api
    with TaskClient(base_url="http://testserver", client=test_client) as client:
        yield client, manager


class TestPythonClientQueueSurface:
    """One method per verb, and no way to set a position.

    The absence is the assertion worth writing down: a client with a generic position
    setter would let a caller choose a number, which is the one thing the whole design
    exists to prevent, and nothing about the resulting corruption would name the client
    as its cause.
    """

    def test_it_offers_no_generic_position_setter(self) -> None:
        from agentjobs.client import TaskOperations

        names = set(dir(TaskOperations)) | set(dir(TaskClient))
        assert not [name for name in names if "position" in name.lower()]

    def test_queue_returns_the_ordered_listing(self, client_for) -> None:
        client, manager = client_for
        make(manager, "task-a")

        listing = client.queue()

        assert [band["band"] for band in listing["bands"]] == [
            "critical",
            "high",
            "medium",
            "low",
        ]
        assert listing["repair_command"] == REPAIR_COMMAND

    def test_explain_next_task_returns_the_section_9_shape(self, client_for) -> None:
        client, manager = client_for
        make(manager, "task-a")

        explanation = client.explain_next_task()

        assert set(explanation) == {
            "task",
            "band",
            "queue_position",
            "empty_bands_above",
            "skipped",
        }
        assert explanation["task"] == "task-a"

    def test_queue_move_reorders_and_reports_the_new_task(self, client_for) -> None:
        client, manager = client_for
        first, second = (make(manager, name) for name in ("task-a", "task-b"))

        result = client.operations.queue_move(
            second,
            actor="Ada",
            operation_id="op-1",
            expected_revision=revision(manager, second),
            top=True,
        )

        assert result.replayed is False
        assert result.task.id == second
        assert order(manager) == [second, first]

    def test_a_replayed_move_is_reported_as_replayed(self, client_for) -> None:
        client, manager = client_for
        make(manager, "task-a")
        make(manager, "task-b")
        stamp = revision(manager, "task-b")
        kwargs = dict(actor="Ada", operation_id="op-1", expected_revision=stamp, top=True)

        client.operations.queue_move("task-b", **kwargs)
        again = client.operations.queue_move("task-b", **kwargs)

        assert again.replayed is True

    def test_reprioritize_changes_the_band(self, client_for) -> None:
        client, manager = client_for
        make(manager, "task-a", priority=Priority.LOW)

        result = client.operations.reprioritize(
            "task-a",
            actor="Ada",
            operation_id="op-1",
            expected_revision=revision(manager, "task-a"),
            priority=Priority.CRITICAL,
        )

        assert result.task.priority is Priority.CRITICAL

    def test_repair_and_compact_are_reachable(self, client_for) -> None:
        client, manager = client_for
        make(manager, "task-a")

        repaired = client.operations.repair_queue(actor="Ada", operation_id="op-1")
        compacted = client.operations.compact_queue(
            actor="Ada", operation_id="op-2", band=Priority.HIGH
        )

        assert repaired["changed"] is False
        assert compacted["band"] == "high"


# ---------------------------------------------------------------------------
# sc-3 -- the CLI listing, which is what child 6 reviews
# ---------------------------------------------------------------------------


class TestQueueListCommand:
    """`agentjobs queue list` is meant to be read by a person at 80 columns.

    It is not a debug dump: it is the artefact the human ordering pass reviews, so the
    assertions here are about legibility -- the width, the band headings, the marker,
    and the reason being present rather than merely implied by the marker.
    """

    def test_prints_the_backlog_band_by_band_in_order(self, project, monkeypatch) -> None:
        root, manager = project
        make(manager, "task-a", priority=Priority.HIGH)
        make(manager, "task-b", priority=Priority.HIGH)
        make(manager, "task-c", priority=Priority.LOW)
        monkeypatch.chdir(root)

        result = runner.invoke(cli_app, ["queue", "list"])

        assert result.exit_code == 0
        headings = [line for line in result.output.splitlines() if line[:1].isupper()]
        assert [line.split()[0] for line in headings] == ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
        assert result.output.index("task-a") < result.output.index("task-b")
        assert result.output.index("task-b") < result.output.index("task-c")

    def test_an_empty_band_says_so_rather_than_being_omitted(self, project, monkeypatch) -> None:
        root, manager = project
        make(manager, "task-a", priority=Priority.HIGH)
        monkeypatch.chdir(root)

        result = runner.invoke(cli_app, ["queue", "list"])

        assert "CRITICAL  (empty)" in result.output

    def test_every_line_fits_eighty_columns(self, project, monkeypatch) -> None:
        root, manager = project
        make(
            manager,
            "task-with-an-unreasonably-long-identifier-that-nobody-would-type",
            title="A title long enough that it cannot possibly fit beside anything else "
            "on one line of a terminal that has not been widened",
        )
        monkeypatch.chdir(root)

        result = runner.invoke(cli_app, ["queue", "list"])

        assert result.exit_code == 0
        assert max(len(line) for line in result.output.splitlines()) <= 80

    def test_a_non_claimable_task_is_marked_and_its_reason_shown(
        self, project, monkeypatch
    ) -> None:
        root, manager = project
        parent = make(manager, "task-a")
        make(manager, "task-b", parent=parent)
        monkeypatch.chdir(root)

        result = runner.invoke(cli_app, ["queue", "list"])

        marked = [line for line in result.output.splitlines() if parent in line]
        assert marked and marked[0].startswith("!")
        assert "not claimable: has 1 open child" in result.output

    def test_a_claimable_task_carries_no_marker_and_no_reason(self, project, monkeypatch) -> None:
        root, manager = project
        make(manager, "task-a")
        monkeypatch.chdir(root)

        result = runner.invoke(cli_app, ["queue", "list"])

        line = next(line for line in result.output.splitlines() if "task-a" in line)
        assert line.startswith(" ")
        assert "not claimable" not in result.output

    def test_the_band_filter_shows_one_band(self, project, monkeypatch) -> None:
        root, manager = project
        make(manager, "task-a", priority=Priority.HIGH)
        make(manager, "task-b", priority=Priority.LOW)
        monkeypatch.chdir(root)

        result = runner.invoke(cli_app, ["queue", "list", "--band", "high"])

        assert "task-a" in result.output
        assert "task-b" not in result.output

    def test_the_claimable_filter_hides_what_cannot_be_taken(self, project, monkeypatch) -> None:
        root, manager = project
        parent = make(manager, "task-a")
        make(manager, "task-b", parent=parent)
        monkeypatch.chdir(root)

        result = runner.invoke(cli_app, ["queue", "list", "--claimable"])

        assert parent not in result.output
        assert "task-b" in result.output

    def test_an_empty_project_says_so(self, project, monkeypatch) -> None:
        root, _ = project
        monkeypatch.chdir(root)
        result = runner.invoke(cli_app, ["queue", "list", "--claimable"])
        assert "No tasks in the queue." in result.output


class TestQueueMutationCommands:
    """`move`, `reprioritize` and `compact` on the command line."""

    def test_move_top_reorders_the_band(self, project, monkeypatch) -> None:
        root, manager = project
        first, second = (make(manager, name) for name in ("task-a", "task-b"))
        monkeypatch.chdir(root)

        result = runner.invoke(cli_app, ["queue", "move", second, "--top"])

        assert result.exit_code == 0
        assert order(manager) == [second, first]

    def test_move_before_a_neighbour(self, project, monkeypatch) -> None:
        root, manager = project
        first, second, third = (make(manager, name) for name in ("task-a", "task-b", "task-c"))
        monkeypatch.chdir(root)

        runner.invoke(cli_app, ["queue", "move", third, "--before", second])

        assert order(manager) == [first, third, second]

    def test_two_placements_exit_non_zero_and_write_nothing(self, project, monkeypatch) -> None:
        root, manager = project
        first, second = (make(manager, name) for name in ("task-a", "task-b"))
        before = position(manager, second)
        monkeypatch.chdir(root)

        result = runner.invoke(cli_app, ["queue", "move", second, "--top", "--before", first])

        assert result.exit_code == 1
        assert "exactly one placement" in result.output
        assert position(manager, second) == before

    def test_moving_a_task_that_does_not_exist_exits_non_zero(self, project, monkeypatch) -> None:
        root, _ = project
        monkeypatch.chdir(root)
        result = runner.invoke(cli_app, ["queue", "move", "task-nope", "--top"])
        assert result.exit_code == 1

    def test_reprioritize_changes_the_band(self, project, monkeypatch) -> None:
        root, manager = project
        make(manager, "task-a", priority=Priority.LOW)
        monkeypatch.chdir(root)

        result = runner.invoke(cli_app, ["queue", "reprioritize", "task-a", "--to", "critical"])

        assert result.exit_code == 0
        task = manager.get_task("task-a")
        assert task is not None and task.priority is Priority.CRITICAL

    def test_compact_renumbers_to_round_hundreds(self, project, monkeypatch) -> None:
        root, manager = project
        first, second, third = (make(manager, name) for name in ("task-a", "task-b", "task-c"))
        monkeypatch.chdir(root)
        runner.invoke(cli_app, ["queue", "move", third, "--top"])

        result = runner.invoke(cli_app, ["queue", "compact", "high"])

        assert result.exit_code == 0
        assert [position(manager, task_id) for task_id in order(manager)] == [100, 200, 300]

    def test_compact_names_each_task_once_on_the_command_line_too(
        self, project, monkeypatch
    ) -> None:
        """Same defect, same fix, separate surface: neither reads the other's code."""
        root, manager = project
        for name in ("task-a", "task-b", "task-c", "task-d"):
            make(manager, name)
        manager.close_task("task-c", actor="bot", outcome=Outcome.COMPLETED)
        monkeypatch.chdir(root)

        result = runner.invoke(cli_app, ["queue", "compact", "high"])

        assert result.exit_code == 0
        for name in ("task-a", "task-b", "task-d"):
            assert result.output.count(name) == 1
        assert "Renumbered 3 task(s)" in result.output

    def test_compacting_an_already_compact_band_says_so(self, project, monkeypatch) -> None:
        root, manager = project
        make(manager, "task-a")
        monkeypatch.chdir(root)

        result = runner.invoke(cli_app, ["queue", "compact", "high"])

        assert result.exit_code == 0
        assert "already compact" in result.output

    def test_an_unattributable_move_is_refused(self, project, monkeypatch) -> None:
        """No fallback to an OS username: either say who you are or the command stops."""
        root, manager = project
        make(manager, "task-a")
        config = yaml.safe_load((root / ".agentjobs" / "config.yaml").read_text(encoding="utf-8"))
        del config["default_user"]
        (root / ".agentjobs" / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
        monkeypatch.chdir(root)

        result = runner.invoke(cli_app, ["queue", "move", "task-a", "--top"])

        assert result.exit_code == 1
        assert "No actor to attribute this to" in result.output


class TestNextCommand:
    """`agentjobs next`, and `--why` printing design section 9."""

    def test_names_the_task_its_band_and_its_position(self, project, monkeypatch) -> None:
        root, manager = project
        make(manager, "task-a", priority=Priority.MEDIUM)
        monkeypatch.chdir(root)

        result = runner.invoke(cli_app, ["next"])

        assert result.exit_code == 0
        assert "task-a" in result.output
        assert f"[medium/{position(manager, 'task-a')}]" in result.output

    def test_without_why_it_does_not_explain(self, project, monkeypatch) -> None:
        root, manager = project
        parent = make(manager, "task-a")
        make(manager, "task-b", parent=parent)
        monkeypatch.chdir(root)

        result = runner.invoke(cli_app, ["next"])

        assert "why each was skipped" not in result.output

    def test_why_lists_what_was_skipped_and_the_rule_that_did_it(
        self, project, monkeypatch
    ) -> None:
        root, manager = project
        parent = make(manager, "task-a")
        make(manager, "task-b", parent=parent)
        monkeypatch.chdir(root)

        result = runner.invoke(cli_app, ["next", "--why"])

        assert result.exit_code == 0
        assert "task-b" in result.output
        assert parent in result.output
        assert "has 1 open child" in result.output

    def test_why_names_the_empty_bands_above_the_winner(self, project, monkeypatch) -> None:
        root, manager = project
        make(manager, "task-a", priority=Priority.MEDIUM)
        monkeypatch.chdir(root)

        result = runner.invoke(cli_app, ["next", "--why"])

        assert "Empty bands above: critical, high" in result.output

    def test_nothing_claimable_says_so_and_still_exits_zero(self, project, monkeypatch) -> None:
        root, manager = project
        make(manager, "task-a", lifecycle=Lifecycle.DRAFT)
        monkeypatch.chdir(root)

        result = runner.invoke(cli_app, ["next", "--why"])

        assert result.exit_code == 0
        assert "Nothing is claimable right now." in result.output
        assert "task-a" in result.output
