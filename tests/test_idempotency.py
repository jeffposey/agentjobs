"""Durable idempotency, creation locking, optimistic revisions, and error codes.

A client that times out cannot tell a lost request from a lost response, so it
retries. These tests are about what happens when it does, and about the two things
that make the answer trustworthy: the replay marker lives in the task file, so it
survives every process restarting, and the check happens inside the same lock as the
write, so it cannot lose a race with a concurrent writer.
"""

from __future__ import annotations

import concurrent.futures
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Tuple

import pytest
import yaml
from fastapi.testclient import TestClient

from agentjobs.api.dependencies import TASKS_DIR_ENV, reset_dependency_cache
from agentjobs.api.main import app
from agentjobs.client import TaskClient, TaskClientError
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Ball, BallReason, Lifecycle, LogEntryType, Outcome
from agentjobs.operations import (
    OPERATION_KEY,
    Operation,
    OperationConflictError,
    RevisionConflictError,
    check_revision,
    fingerprint,
    stamp,
)
from agentjobs.projects import ProjectRegistry
from agentjobs.storage import TaskStorage

ACTORS = [
    {"name": "Ada", "kind": "human", "display_name": "Ada Lovelace"},
    {"name": "bot", "kind": "agent", "display_name": "Bot"},
    {"name": "other", "kind": "agent", "display_name": "Other Bot"},
]

OP_A = "11111111-1111-4111-8111-111111111111"
OP_B = "22222222-2222-4222-8222-222222222222"


def write_config(root: Path, name: str) -> None:
    """Give a project directory an actor vocabulary."""
    (root / ".agentjobs").mkdir(parents=True, exist_ok=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "project_name": name,
                "tasks_directory": "tasks",
                "actors": ACTORS,
                "default_user": "Ada",
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def manager(tmp_path: Path) -> TaskManager:
    """A manager over a fresh temp project, used directly for the storage-level tests."""
    write_config(tmp_path, "Temp")
    return TaskManager(TaskStorage(tmp_path / "tasks"))


@pytest.fixture()
def service(tmp_path: Path, monkeypatch) -> Iterator[Tuple[TaskClient, TaskManager]]:
    """The real app over one registered project, driven through TaskClient."""
    monkeypatch.setenv("AGENTJOBS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(TASKS_DIR_ENV, raising=False)
    monkeypatch.delenv("AGENTJOBS_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    reset_dependency_cache()

    write_config(tmp_path / "solo", "Solo")
    ProjectRegistry(home=tmp_path / "home").add(tmp_path / "solo", project_id="solo")
    manager = TaskManager(TaskStorage(tmp_path / "solo" / "tasks"))

    with TestClient(app) as http:
        yield TaskClient("http://testserver", client=http).for_project("solo"), manager

    reset_dependency_cache()


def ready_task(manager: TaskManager, task_id: str = "task-001-work"):
    """A ready task with no dependencies."""
    return manager.create_task(
        id=task_id,
        title="Work",
        description="Do the thing.",
        category="general",
        lifecycle=Lifecycle.READY,
    )


# ---------------------------------------------------------------------------
# The primitives
# ---------------------------------------------------------------------------
class TestFingerprint:
    def test_the_same_intent_digests_the_same(self):
        assert fingerprint("claim", "bot", {"a": 1}) == fingerprint("claim", "bot", {"a": 1})

    def test_a_different_verb_actor_or_payload_digests_differently(self):
        base = fingerprint("claim", "bot", {"a": 1})
        assert fingerprint("release", "bot", {"a": 1}) != base
        assert fingerprint("claim", "other", {"a": 1}) != base
        assert fingerprint("claim", "bot", {"a": 2}) != base

    def test_omitting_an_optional_equals_sending_it_as_null(self):
        """Otherwise a client that stops sending body=None looks like a new operation."""
        assert fingerprint("close", "bot", {"outcome": "completed", "body": None}) == fingerprint(
            "close", "bot", {"outcome": "completed"}
        )

    def test_key_order_does_not_change_the_digest(self):
        assert fingerprint("x", "bot", {"a": 1, "b": 2}) == fingerprint(
            "x", "bot", {"b": 2, "a": 1}
        )

    def test_a_value_that_will_not_serialise_still_digests(self):
        """Refusing to fingerprint would turn a legitimate retry into a hard failure."""
        moment = datetime(2026, 8, 10, tzinfo=timezone.utc)
        assert fingerprint("x", "bot", {"when": moment}) == fingerprint(
            "x", "bot", {"when": moment}
        )


class TestStamp:
    def test_a_caller_cannot_forge_an_operation_marker(self):
        """Forging one would make a real write look like a replay that already happened."""
        with pytest.raises(ValueError, match="reserved"):
            stamp({OPERATION_KEY: {"id": "fake"}}, None)

    def test_data_without_an_operation_passes_through(self):
        assert stamp({"a": 1}, None) == {"a": 1}

    def test_an_operation_is_recorded_with_its_fingerprint(self):
        operation = Operation(id=OP_A, kind="claim", actor="bot", payload={})
        marker = stamp(None, operation)[OPERATION_KEY]
        assert marker["id"] == OP_A
        assert marker["kind"] == "claim"
        assert marker["fingerprint"] == operation.fingerprint


class TestCheckRevision:
    def test_a_matching_revision_passes(self, manager):
        task = ready_task(manager)
        check_revision(task, task.updated)

    def test_a_stale_revision_is_refused_and_carries_the_current_task(self, manager):
        task = ready_task(manager)
        stale = task.updated - timedelta(seconds=5)
        with pytest.raises(RevisionConflictError) as caught:
            check_revision(task, stale)
        assert caught.value.current_task is task

    def test_an_iso_string_is_accepted(self, manager):
        task = ready_task(manager)
        check_revision(task, task.updated.isoformat())

    def test_a_value_that_is_not_a_timestamp_is_refused(self, manager):
        task = ready_task(manager)
        with pytest.raises(RevisionConflictError, match="not a timestamp"):
            check_revision(task, "yesterday")

    def test_no_expectation_means_no_check(self, manager):
        check_revision(ready_task(manager), None)


# ---------------------------------------------------------------------------
# ac-1: replay
# ---------------------------------------------------------------------------
class TestReplay:
    def test_a_repeated_claim_writes_nothing_the_second_time(self, manager):
        task = ready_task(manager)

        first = manager.claim_task(task.id, agent="bot", operation_id=OP_A)
        second = manager.claim_task(task.id, agent="bot", operation_id=OP_A)

        assert first.assignment.owner == "bot"
        assert second.assignment.owner == "bot"
        assert len(second.log) == len(first.log)
        assert second.updated == first.updated

    def test_without_an_operation_id_a_repeated_claim_still_refuses(self, manager):
        """Replay is opt-in; the old precondition is what protects an unmarked retry."""
        task = ready_task(manager)
        manager.claim_task(task.id, agent="bot")

        with pytest.raises(ValueError, match="not available to claim"):
            manager.claim_task(task.id, agent="bot")

    def test_a_repeated_log_append_does_not_duplicate_the_entry(self, manager):
        task = ready_task(manager)

        manager.add_log_entry(
            task.id, actor="bot", type=LogEntryType.NOTE, body="x", operation_id=OP_A
        )
        after = manager.add_log_entry(
            task.id, actor="bot", type=LogEntryType.NOTE, body="x", operation_id=OP_A
        )

        assert [entry.body for entry in after.log].count("x") == 1

    def test_two_different_appends_both_land(self, manager):
        task = ready_task(manager)

        manager.add_log_entry(
            task.id, actor="bot", type=LogEntryType.NOTE, body="one", operation_id=OP_A
        )
        after = manager.add_log_entry(
            task.id, actor="bot", type=LogEntryType.NOTE, body="two", operation_id=OP_B
        )

        assert [entry.body for entry in after.log] == ["one", "two"]

    def test_a_repeated_handoff_does_not_append_twice(self, manager):
        task = ready_task(manager)
        manager.claim_task(task.id, agent="bot")

        first = manager.handoff(
            task.id,
            actor="bot",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Look at it.",
            operation_id=OP_A,
        )
        second = manager.handoff(
            task.id,
            actor="bot",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Look at it.",
            operation_id=OP_A,
        )

        assert len(second.log) == len(first.log)

    def test_a_repeated_close_does_not_reject_itself_as_already_closed(self, manager):
        """The whole point: a retry must succeed, not hit the precondition."""
        task = ready_task(manager)
        manager.claim_task(task.id, agent="bot")

        manager.close_task(task.id, actor="bot", outcome=Outcome.COMPLETED, operation_id=OP_A)
        replayed = manager.close_task(
            task.id, actor="bot", outcome=Outcome.COMPLETED, operation_id=OP_A
        )

        assert replayed.lifecycle is Lifecycle.CLOSED
        assert replayed.outcome is Outcome.COMPLETED

    def test_a_repeated_release_is_a_no_op(self, manager):
        task = ready_task(manager)
        manager.claim_task(task.id, agent="bot")

        first = manager.release_task(task.id, actor="bot", operation_id=OP_A)
        second = manager.release_task(task.id, actor="bot", operation_id=OP_A)

        assert second.lifecycle is Lifecycle.READY
        assert len(second.log) == len(first.log)

    def test_a_repeated_content_update_does_not_append_twice(self, manager):
        task = ready_task(manager)

        first = manager.update_task(task.id, operation_id=OP_A, actor="bot", title="Renamed")
        second = manager.update_task(task.id, operation_id=OP_A, actor="bot", title="Renamed")

        assert second.title == "Renamed"
        assert len(second.log) == len(first.log)

    def test_replay_survives_a_restart_of_everything(self, tmp_path):
        """The marker is in the file, so nothing in memory has to survive.

        A fresh manager over the same directory stands in for the MCP process, the
        API process, and the machine all restarting between the write and the retry --
        which is exactly when a client is most likely to resend.
        """
        write_config(tmp_path, "Restart")
        first_manager = TaskManager(TaskStorage(tmp_path / "tasks"))
        task = ready_task(first_manager)
        first_manager.claim_task(task.id, agent="bot", operation_id=OP_A)

        second_manager = TaskManager(TaskStorage(tmp_path / "tasks"))
        replayed = second_manager.claim_task(task.id, agent="bot", operation_id=OP_A)

        assert replayed.assignment.owner == "bot"
        assert sum(1 for entry in replayed.log if entry.type is LogEntryType.TRANSITION) == 1


# ---------------------------------------------------------------------------
# ac-2: conflict
# ---------------------------------------------------------------------------
class TestOperationConflict:
    def test_reusing_an_id_for_a_different_payload_is_refused(self, manager):
        task = ready_task(manager)
        manager.add_log_entry(
            task.id, actor="bot", type=LogEntryType.NOTE, body="one", operation_id=OP_A
        )

        with pytest.raises(OperationConflictError):
            manager.add_log_entry(
                task.id, actor="bot", type=LogEntryType.NOTE, body="different", operation_id=OP_A
            )

    def test_reusing_an_id_for_a_different_actor_is_refused(self, manager):
        task = ready_task(manager)
        manager.add_log_entry(
            task.id, actor="bot", type=LogEntryType.NOTE, body="x", operation_id=OP_A
        )

        with pytest.raises(OperationConflictError):
            manager.add_log_entry(
                task.id, actor="other", type=LogEntryType.NOTE, body="x", operation_id=OP_A
            )

    def test_reusing_an_id_for_a_different_verb_is_refused(self, manager):
        task = ready_task(manager)
        manager.claim_task(task.id, agent="bot", operation_id=OP_A)

        with pytest.raises(OperationConflictError):
            manager.release_task(task.id, actor="bot", operation_id=OP_A)

    def test_a_conflict_writes_nothing(self, manager):
        task = ready_task(manager)
        before = manager.add_log_entry(
            task.id, actor="bot", type=LogEntryType.NOTE, body="one", operation_id=OP_A
        )

        with pytest.raises(OperationConflictError):
            manager.add_log_entry(
                task.id, actor="bot", type=LogEntryType.NOTE, body="two", operation_id=OP_A
            )

        after = manager.get_task(task.id)
        assert len(after.log) == len(before.log)
        assert after.updated == before.updated


# ---------------------------------------------------------------------------
# ac-3: creation
# ---------------------------------------------------------------------------
class TestCreation:
    def test_a_retried_create_resolves_to_the_original_task(self, manager):
        first = manager.create_task(
            title="Once", description="d", category="general", operation_id=OP_A, actor="bot"
        )
        second = manager.create_task(
            title="Once", description="d", category="general", operation_id=OP_A, actor="bot"
        )

        assert second.id == first.id
        assert len(manager.list_tasks()) == 1

    def test_a_retried_auto_id_create_does_not_produce_a_second_task(self, manager):
        """Without the creation lock and marker, the retry would generate a new id."""
        first = manager.create_task(
            title="Auto", description="d", category="general", operation_id=OP_A, actor="bot"
        )
        second = manager.create_task(
            title="Auto", description="d", category="general", operation_id=OP_A, actor="bot"
        )

        assert first.id == second.id
        assert len(manager.list_tasks()) == 1

    def test_reusing_a_creation_id_for_different_arguments_is_refused(self, manager):
        manager.create_task(
            title="One", description="d", category="general", operation_id=OP_A, actor="bot"
        )

        with pytest.raises(OperationConflictError):
            manager.create_task(
                title="Two", description="d", category="general", operation_id=OP_A, actor="bot"
            )

    def test_a_create_without_an_operation_id_keeps_an_empty_log(self, manager):
        """Existing callers must not suddenly find a log entry they never wrote."""
        task = manager.create_task(title="Plain", description="d", category="general")

        assert task.log == []

    def test_a_create_with_an_operation_id_records_who_made_it(self, manager):
        task = manager.create_task(
            title="Marked", description="d", category="general", operation_id=OP_A, actor="bot"
        )

        assert task.log[0].actor == "bot"
        assert task.log[0].data[OPERATION_KEY]["id"] == OP_A

    def test_concurrent_auto_id_creates_are_serialised(self, tmp_path):
        """Two writers must not both take the same generated id."""
        write_config(tmp_path, "Race")
        storage = TaskStorage(tmp_path / "tasks")

        def create(index: int) -> str:
            local = TaskManager(TaskStorage(tmp_path / "tasks"))
            return local.create_task(
                title=f"Racer {index}",
                description="d",
                category="general",
                operation_id=f"33333333-3333-4333-8333-{index:012d}",
                actor="bot",
            ).id

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(create, range(8)))

        assert len(set(ids)) == 8
        assert len(storage.list_tasks()) == 8


# ---------------------------------------------------------------------------
# ac-4: revisions
# ---------------------------------------------------------------------------
class TestRevisions:
    def test_a_stale_handoff_is_refused(self, manager):
        task = ready_task(manager)
        manager.claim_task(task.id, agent="bot")
        stale = manager.get_task(task.id).updated
        manager.add_log_entry(task.id, actor="bot", type=LogEntryType.NOTE, body="moved on")

        with pytest.raises(RevisionConflictError):
            manager.handoff(
                task.id,
                actor="bot",
                ball=Ball.HUMAN,
                ball_reason=BallReason.REVIEW,
                ball_prompt="Look.",
                expected_revision=stale,
            )

    def test_a_current_handoff_succeeds(self, manager):
        task = ready_task(manager)
        manager.claim_task(task.id, agent="bot")
        current = manager.get_task(task.id).updated

        after = manager.handoff(
            task.id,
            actor="bot",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Look.",
            expected_revision=current,
        )

        assert after.ball is Ball.HUMAN

    def test_a_stale_close_is_refused_and_changes_nothing(self, manager):
        task = ready_task(manager)
        manager.claim_task(task.id, agent="bot")
        stale = manager.get_task(task.id).updated
        manager.add_log_entry(task.id, actor="bot", type=LogEntryType.NOTE, body="moved on")

        with pytest.raises(RevisionConflictError):
            manager.close_task(
                task.id, actor="bot", outcome=Outcome.COMPLETED, expected_revision=stale
            )

        assert manager.get_task(task.id).lifecycle is Lifecycle.ACTIVE

    def test_a_stale_content_update_is_refused(self, manager):
        task = ready_task(manager)
        stale = task.updated
        manager.add_log_entry(task.id, actor="bot", type=LogEntryType.NOTE, body="moved on")

        with pytest.raises(RevisionConflictError):
            manager.update_task(task.id, expected_revision=stale, title="Nope")

        assert manager.get_task(task.id).title == "Work"

    def test_omitting_the_revision_preserves_last_write_wins(self, manager):
        """Existing callers pass no revision and must keep working unchanged."""
        task = ready_task(manager)
        manager.add_log_entry(task.id, actor="bot", type=LogEntryType.NOTE, body="moved on")

        assert manager.update_task(task.id, title="Renamed").title == "Renamed"


# ---------------------------------------------------------------------------
# ac-5: the REST and client contract
# ---------------------------------------------------------------------------
class TestServiceContract:
    def test_the_envelope_reports_a_first_call_as_not_replayed(self, service):
        client, manager = service
        task = ready_task(manager)

        result = client.operations.claim(task.id, actor="bot", operation_id=OP_A)

        assert result.replayed is False
        assert result.project_id == "solo"
        assert result.operation_id == OP_A
        assert result.task.assignment.owner == "bot"
        assert result.warnings == []

    def test_the_envelope_reports_a_retry_as_replayed(self, service):
        client, manager = service
        task = ready_task(manager)

        client.operations.claim(task.id, actor="bot", operation_id=OP_A)
        result = client.operations.claim(task.id, actor="bot", operation_id=OP_A)

        assert result.replayed is True

    def test_without_the_envelope_the_response_is_the_bare_task(self, service):
        """The compatibility promise: an unchanged caller sees an unchanged shape."""
        client, manager = service
        task = ready_task(manager)

        assert client.claim_task(task.id, agent="bot").id == task.id

    def test_an_operation_conflict_carries_its_code(self, service):
        client, manager = service
        task = ready_task(manager)
        client.operations.append_log(task.id, actor="bot", operation_id=OP_A, body="one")

        with pytest.raises(TaskClientError) as caught:
            client.operations.append_log(task.id, actor="bot", operation_id=OP_A, body="two")

        assert caught.value.code == "operation_conflict"
        assert caught.value.retryable is False
        assert caught.value.suggested_action

    def test_a_revision_conflict_returns_the_current_task(self, service):
        client, manager = service
        task = ready_task(manager)
        client.operations.claim(task.id, actor="bot", operation_id=OP_A)
        stale = task.updated

        with pytest.raises(TaskClientError) as caught:
            client.operations.handoff(
                task.id,
                actor="bot",
                operation_id=OP_B,
                expected_revision=stale,
                ball=Ball.HUMAN,
                ball_reason=BallReason.REVIEW,
                ball_prompt="Look.",
            )

        assert caught.value.code == "revision_conflict"
        assert caught.value.current_task is not None
        assert caught.value.current_task["id"] == task.id

    def test_an_unknown_actor_carries_its_code_and_field(self, service):
        client, manager = service
        task = ready_task(manager)

        with pytest.raises(TaskClientError) as caught:
            client.operations.claim(task.id, actor="nobody", operation_id=OP_A)

        assert caught.value.code == "unknown_actor"
        assert caught.value.field_errors[0]["path"] == "actor"

    def test_a_missing_task_is_not_found(self, service):
        client, _ = service

        with pytest.raises(TaskClientError) as caught:
            client.operations.claim("task-404-absent", actor="bot", operation_id=OP_A)

        assert caught.value.code == "task_not_found"

    def test_a_blocked_claim_reports_dependency_blocked(self, service):
        """An unmet `needs` -- which since task-164 is the only thing that blocks a claim.

        This was written with an umbrella and its open child. That is no longer a
        refusal at all: an epic is claimable, and the claim hands over supervision.
        """
        client, manager = service
        blocker = manager.create_task(
            id="task-900-blocker",
            title="Blocker",
            description="d",
            category="general",
            lifecycle=Lifecycle.READY,
        )
        dependent = manager.create_task(
            id="task-901-dependent",
            title="Dependent",
            description="d",
            category="general",
            lifecycle=Lifecycle.READY,
            dependencies=[{"task": blocker.id, "type": "needs"}],
        )

        with pytest.raises(TaskClientError) as caught:
            client.operations.claim(dependent.id, actor="bot", operation_id=OP_A)

        assert caught.value.code == "dependency_blocked"

    def test_a_refused_transition_reports_invalid_transition(self, service):
        client, manager = service
        task = ready_task(manager)
        client.operations.claim(task.id, actor="bot", operation_id=OP_A)

        with pytest.raises(TaskClientError) as caught:
            client.operations.claim(task.id, actor="other", operation_id=OP_B)

        assert caught.value.code == "invalid_transition"
        assert caught.value.retryable is False

    def test_a_retried_create_through_the_service_makes_one_task(self, service):
        client, manager = service

        first = client.operations.create(
            actor="bot", operation_id=OP_A, title="Once", description="d", summary="s"
        )
        second = client.operations.create(
            actor="bot", operation_id=OP_A, title="Once", description="d", summary="s"
        )

        assert first.id == second.id
        assert len(manager.list_tasks()) == 1

    def test_a_content_update_through_the_service_respects_the_revision(self, service):
        client, manager = service
        task = ready_task(manager)

        updated = client.operations.update_content(
            task.id,
            actor="bot",
            operation_id=OP_A,
            expected_revision=task.updated,
            title="Renamed",
        )

        assert updated.title == "Renamed"

        with pytest.raises(TaskClientError):
            client.operations.update_content(
                task.id,
                actor="bot",
                operation_id=OP_B,
                expected_revision=task.updated,
                title="Again",
            )


# ---------------------------------------------------------------------------
# ac-6: nothing that already worked stopped working
# ---------------------------------------------------------------------------
class TestExistingBehaviourPreserved:
    def test_racing_claims_still_produce_exactly_one_winner(self, tmp_path):
        write_config(tmp_path, "Race")
        seed = TaskManager(TaskStorage(tmp_path / "tasks"))
        task = ready_task(seed)

        def claim(index: int):
            local = TaskManager(TaskStorage(tmp_path / "tasks"))
            try:
                local.claim_task(task.id, agent="bot")
                return True
            except ValueError:
                return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(claim, range(8)))

        assert sum(results) == 1

    def test_concurrent_independent_appends_all_land(self, tmp_path):
        """Appends carry no revision precisely so they do not conflict with each other."""
        write_config(tmp_path, "Appends")
        seed = TaskManager(TaskStorage(tmp_path / "tasks"))
        task = ready_task(seed)

        def append(index: int) -> None:
            local = TaskManager(TaskStorage(tmp_path / "tasks"))
            local.add_log_entry(
                task.id,
                actor="bot",
                type=LogEntryType.PROGRESS,
                body=f"entry {index}",
                operation_id=f"44444444-4444-4444-8444-{index:012d}",
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(append, range(8)))

        bodies = {entry.body for entry in seed.get_task(task.id).log}
        assert {f"entry {index}" for index in range(8)} <= bodies

    def test_a_transition_entry_still_cannot_be_authored_directly(self, manager):
        task = ready_task(manager)

        with pytest.raises(ValueError, match="appended by the manager"):
            manager.add_log_entry(task.id, actor="bot", type=LogEntryType.TRANSITION, body="x")
