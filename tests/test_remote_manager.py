"""The CLI's manager, reached over the service instead of over the store.

Two things are worth testing here and they are different. The parsers turn the API's
``as_dict`` forms back into the dataclasses the CLI renders, and a round trip is the
only honest check of those -- a hand-written expectation would pass while both ends
drifted together. Everything else is exercised against the real application over an
in-process transport, because what is most likely to be wrong is not the parsing but
whether a verb reaches the route it thinks it does.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest
import yaml
from starlette.testclient import TestClient

from agentjobs.api.dependencies import reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.client import TaskClient
from agentjobs.manager import (
    MoveOutcome,
    NextExplanation,
    QueueBand,
    QueueEntry,
    QueueListing,
    QueueMoveProvenance,
    SkippedTask,
    TaskManager,
)
from agentjobs.models_v2 import (
    Assignment,
    Ball,
    BallReason,
    Lifecycle,
    LogEntry,
    LogEntryType,
    Outcome,
    Priority,
    Spec,
    Task,
)
from agentjobs.projects import Project, ProjectRegistry
from agentjobs.queue import Placement, QueueProblem
from agentjobs.queue_check import QueueWarning
from agentjobs.remote_manager import (
    RemoteStoreUnsupported,
    RemoteTaskManager,
    _entry,
    _explanation,
    _listing,
    _placement,
    _problem,
    _provenance,
    _warning,
)
from agentjobs.storage import TaskStorage

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


class TestTheParsersRoundTrip:
    """Each parser is the inverse of an ``as_dict`` that already exists."""

    def test_a_queue_entry_survives(self) -> None:
        entry = QueueEntry(
            task="task-001",
            title="Something",
            queue_position=100,
            lifecycle="ready",
            ball="agent",
            claimable=True,
            reason=None,
            last_move=QueueMoveProvenance(
                actor="claude",
                kind="agent",
                at="2026-09-01T12:00:00Z",
                body="Moved to the top.",
                anchor="strong",
            ),
        )
        assert _entry(entry.as_dict()) == entry

    def test_an_entry_that_was_never_moved_survives(self) -> None:
        entry = QueueEntry(
            task="task-002",
            title="Untouched",
            queue_position=200,
            lifecycle="draft",
            ball="human",
            claimable=False,
            reason="it is a draft",
        )
        parsed = _entry(entry.as_dict())
        # None, not a provenance naming nobody: those are different facts.
        assert parsed.last_move is None
        assert parsed == entry

    def test_a_provenance_survives(self) -> None:
        move = QueueMoveProvenance(
            actor="Jeff Posey",
            kind="human",
            at="2026-09-01T00:00:00Z",
            body="Ahead of 45.",
            anchor=None,
        )
        assert _provenance(move.as_dict()) == move

    def test_a_listing_survives(self) -> None:
        listing = QueueListing(
            bands=(
                QueueBand(
                    band="high",
                    entries=(
                        QueueEntry(
                            task="task-001",
                            title="First",
                            queue_position=100,
                            lifecycle="ready",
                            ball="agent",
                            claimable=True,
                            reason=None,
                        ),
                    ),
                ),
            ),
            problems=(
                QueueProblem(kind="duplicate", band="high", task_ids=("a", "b"), position=100),
            ),
        )
        assert _listing(listing.as_dict()) == listing

    def test_an_explanation_survives(self) -> None:
        explanation = NextExplanation(
            task="task-001",
            band="high",
            queue_position=100,
            empty_bands_above=("critical",),
            skipped=(SkippedTask(task="task-002", queue_position=50, reason="it is claimed"),),
        )
        assert _explanation(explanation.as_dict()) == explanation

    def test_a_problem_and_a_warning_survive(self) -> None:
        problem = QueueProblem(kind="missing", band="low", task_ids=("task-003",), position=None)
        assert (
            _problem(
                {
                    "kind": problem.kind,
                    "band": problem.band,
                    "tasks": list(problem.task_ids),
                    "position": problem.position,
                }
            )
            == problem
        )

        warning = QueueWarning(kind="ahead_of_needs", message="It jumped its needs.", tasks=("a",))
        assert _warning(warning.as_dict()) == warning

    def test_a_placement_survives_and_absence_stays_absent(self) -> None:
        placement = Placement(kind=Placement.BEFORE, target="task-009")
        assert _placement(placement.as_data()) == placement
        assert _placement(None) is None


# ----- against the real application ------------------------------------------


def _seed(root: Path) -> None:
    """A project with two ready tasks, written by the file backend."""
    (root / ".agentjobs").mkdir(parents=True, exist_ok=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "project_name": "Sandbox",
                "tasks_directory": "tasks",
                "default_user": "Jeff Posey",
                "actors": [
                    {"name": "Jeff Posey", "kind": "human"},
                    {"name": "claude", "kind": "agent"},
                ],
            }
        ),
        encoding="utf-8",
    )
    storage = TaskStorage(root / "tasks")
    for index, task_id in enumerate(("task-001", "task-002"), start=1):
        storage.save_task(
            Task(
                id=task_id,
                title=f"Task {index}",
                created=NOW,
                updated=NOW,
                lifecycle=Lifecycle.READY,
                ball=Ball.AGENT,
                ball_reason=BallReason.AVAILABLE,
                priority=Priority.HIGH,
                queue_position=index * 100,
                category="infrastructure",
                assignment=Assignment(),
                spec=Spec(summary=f"Summary {index}", description="A description."),
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
        )


@pytest.fixture()
def remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[RemoteTaskManager]:
    """A remote manager talking to the real application in this process.

    The transport is ASGI rather than a socket, so the request goes through routing,
    the capability gate and the dependency wiring exactly as a network request does --
    which is the half a hand-rolled fake would skip and the half most likely to be
    wrong.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AGENTJOBS_HOME", str(home))
    root = tmp_path / "sandbox"
    _seed(root)
    ProjectRegistry(home).add(root, project_id="sandbox")
    monkeypatch.chdir(root)
    reset_dependency_cache()

    # Starlette's TestClient *is* a synchronous httpx.Client over the ASGI app, which
    # is what lets the real client speak to the real application without a socket. A
    # bare httpx.ASGITransport cannot: it is async-only.
    connection = TestClient(app)
    client = TaskClient("http://testserver", client=connection, project_id="sandbox")
    yield RemoteTaskManager(client, Project(id="sandbox", name="Sandbox", root=root))
    connection.close()


class TestTheVerbsOverTheService:
    """Each one reaches the route it thinks it does, and returns what a caller expects."""

    def test_reads(self, remote: RemoteTaskManager) -> None:
        assert {task.id for task in remote.list_tasks()} == {"task-001", "task-002"}
        assert remote.get_task("task-001") is not None
        assert remote.get_task("task-999") is None
        chosen = remote.get_next_task()
        assert chosen is not None and chosen.id == "task-001"

    def test_the_claimable_set_is_the_queues_answer(self, remote: RemoteTaskManager) -> None:
        claimable = remote.claimable_tasks()
        assert [task.id for task in claimable] == ["task-001", "task-002"]
        # `/next` is its head, by construction rather than by coincidence.
        head = remote.get_next_task()
        assert head is not None and head.id == claimable[0].id

    def test_claim_then_handoff_then_close(self, remote: RemoteTaskManager) -> None:
        claimed = remote.claim_task("task-001", agent="claude")
        assert claimed.assignment.owner == "claude"

        handed = remote.handoff(
            "task-001",
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Read the diff",
        )
        assert handed.ball is Ball.HUMAN and handed.ball_prompt == "Read the diff"

        closed = remote.close_task("task-001", actor="claude", outcome=Outcome.COMPLETED)
        assert closed.lifecycle is Lifecycle.CLOSED

    def test_a_log_entry_lands_on_the_record(self, remote: RemoteTaskManager) -> None:
        after = remote.add_log_entry(
            "task-002", actor="claude", type=LogEntryType.PROGRESS, body="Made a start."
        )
        assert any(entry.body == "Made a start." for entry in after.log)

    def test_the_queue_listing_comes_back_as_the_dataclasses_the_cli_renders(
        self, remote: RemoteTaskManager
    ) -> None:
        listing = remote.queue_listing()
        assert isinstance(listing, QueueListing)
        assert [band.band for band in listing.bands]
        assert remote.check_queue() == []

    def test_a_move_reports_what_it_did(self, remote: RemoteTaskManager) -> None:
        outcome = remote.move_with_warnings("task-002", actor="Jeff Posey", top=True)
        assert isinstance(outcome, MoveOutcome)
        assert outcome.task.id == "task-002"
        head = remote.get_next_task()
        assert head is not None and head.id == "task-002"

    def test_explain_next_comes_back_structured(self, remote: RemoteTaskManager) -> None:
        explanation = remote.explain_next()
        assert isinstance(explanation, NextExplanation)
        assert explanation.task == "task-001"

    def test_dependency_facts_come_from_the_server(self, remote: RemoteTaskManager) -> None:
        facts = remote.dependency_facts()
        assert facts["task-001"].actionable is True

    def test_a_created_task_gets_its_id_from_the_server(self, remote: RemoteTaskManager) -> None:
        created = remote.create_task(
            actor="claude",
            title="Filed remotely",
            description="Body",
            summary="Summary",
            category="infrastructure",
            priority="high",
        )
        assert created.id not in {"task-001", "task-002"}
        assert remote.get_task(created.id) is not None

    def test_create_supplies_the_summary_the_local_manager_defaults(
        self, remote: RemoteTaskManager
    ) -> None:
        # The CLI never had to pass one: the local manager falls back to the title. The
        # REST surface requires it, so without the same fallback a `create` that had
        # always worked would start failing the moment its project migrated.
        created = remote.create_task(
            actor="claude",
            title="No summary given",
            description="Body",
            category="infrastructure",
            id=None,
        )
        assert created.spec.summary == "No summary given"


class TestEveryVerbIsActuallyCalled:
    """Presence is not enough, and this class exists because it was not.

    ``test_every_method_the_cli_and_dispatch_use_exists_on_both`` passed while
    ``promote_task`` raised ``TypeError`` on its first real call: the client requires an
    ``expected_revision`` that the local manager computes for itself inside the
    transaction, and the facade was not supplying one. A hasattr check cannot see that.
    Each verb below is called once, with the arguments its CLI caller passes.
    """

    def test_promote(self, remote: RemoteTaskManager) -> None:
        created = remote.create_task(
            actor="claude", title="A draft", description="Body", category="infrastructure"
        )
        promoted = remote.promote_task(created.id, actor="claude")
        assert promoted.lifecycle is Lifecycle.READY

    def test_release(self, remote: RemoteTaskManager) -> None:
        remote.claim_task("task-001", agent="claude")
        released = remote.release_task("task-001", actor="claude", body="Not mine after all")
        assert released.ball is Ball.AGENT
        assert released.assignment.owner is None

    def test_update_content(self, remote: RemoteTaskManager) -> None:
        updated = remote.update_task("task-002", actor="claude", effort="about a day")
        assert updated.effort == "about a day"

    def test_progress(self, remote: RemoteTaskManager) -> None:
        after = remote.add_progress_update(
            task_id="task-002", author="claude", summary="Halfway", details="More detail"
        )
        assert any("Halfway" in (entry.body or "") for entry in after.log)

    def test_redact(self, remote: RemoteTaskManager) -> None:
        after = remote.redact(
            "task-002",
            field="spec.description",
            replacement="[removed]",
            reason="it quoted a person",
            actor="Jeff Posey",
        )
        assert after.spec.description == "[removed]"

    def test_repair_and_compact(self, remote: RemoteTaskManager) -> None:
        report = remote.repair_queue()
        # A healthy queue needs no repair, and the report says so rather than churning.
        assert report.assigned == ()
        assert remote.compact_band(Priority.HIGH) is not None

    def test_subtasks(self, remote: RemoteTaskManager) -> None:
        child = remote.create_task(
            actor="claude",
            title="A child",
            description="Body",
            category="infrastructure",
            parent="task-001",
        )
        assert [task.id for task in remote.get_subtasks("task-001")] == [child.id]


class TestWhatItRefuses:
    """A file-shaped question with no answer is refused by name, never guessed at."""

    def test_there_is_no_task_path(self, remote: RemoteTaskManager) -> None:
        # Every caller of this hands the result to git. A plausible path would turn a
        # clear failure into a silent one.
        with pytest.raises(RemoteStoreUnsupported, match="not a file"):
            remote.storage.task_path("task-001")

    def test_there_is_no_tasks_directory(self, remote: RemoteTaskManager) -> None:
        with pytest.raises(RemoteStoreUnsupported, match="storage export"):
            _ = remote.storage.tasks_dir

    def test_a_run_is_not_recorded_over_the_service(self, remote: RemoteTaskManager) -> None:
        # The one part of the manager that deliberately does not cross the wire:
        # recording a dispatch means sending argv, and a dispatch schema carrying argv
        # is what tests/test_dispatch_api.py forbids.
        with pytest.raises(RemoteStoreUnsupported, match="dispatch_manager_for"):
            remote.record_dispatch("task-001", actor="Jeff Posey", run_id="run_1")

    def test_an_id_cannot_be_reserved_in_advance(self, remote: RemoteTaskManager) -> None:
        with pytest.raises(RemoteStoreUnsupported, match="allocated by the server"):
            remote.storage.generate_task_id()


class TestTheSurfacesAgree:
    """A local manager and a remote one answer the same questions the same way."""

    def test_every_method_the_cli_and_dispatch_use_exists_on_both(self) -> None:
        # The union of what `grep 'manager\\.'` finds outside the API. A method missing
        # from the remote side is not a type error anywhere -- it is an AttributeError
        # in whichever command nobody ran after the cutover.
        required = {
            "add_log_entry",
            "add_progress_update",
            "check_queue",
            "claim_task",
            "claimable_tasks",
            "close_task",
            "compact_band",
            "create_task",
            "dependency_facts",
            "explain_next",
            "get_next_task",
            "get_subtasks",
            "get_task",
            "handoff",
            "list_tasks",
            "move_with_warnings",
            "promote_task",
            "queue_listing",
            "record_dispatch",
            "record_dispatch_result",
            "redact",
            "release_task",
            "repair_queue",
            "reprioritize",
            "search_tasks",
            "update_task",
        }
        missing = {name for name in required if not hasattr(RemoteTaskManager, name)}
        assert missing == set(), f"the remote manager is missing {sorted(missing)}"
        absent_locally = {name for name in required if not hasattr(TaskManager, name)}
        assert absent_locally == set(), f"no longer on TaskManager: {sorted(absent_locally)}"
