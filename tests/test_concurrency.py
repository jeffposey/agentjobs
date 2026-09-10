"""The double-claim race, and what closes it.

The important test here is ``test_only_one_of_many_racing_agents_wins``. It drives real
threads at a real store, and it fails against the pre-task-055 implementation -- which
is the only reason to trust it.

``TestTheLockItself`` stood below. It drove the file backend's advisory lock: exclusive
creation, release on an exception, per-task scoping, the Windows delete-pending retry, an
unwritable directory, the stale-lock message, and lock files not being mistaken for
tasks. All seven were about a lock file beside a task file, and both are gone (task-402).
What replaced them is a transaction, and it is asserted where it can be: the serialising
behaviour is what the race above proves, and `tests/test_sqlstore.py` covers the mutate
verbs and the invariants the constraints now hold.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import List

import pytest

from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Ball, BallReason, Lifecycle, Priority, Spec, Task
from agentjobs.sqlstore import SqlTaskStore
from support import task_store

NOW = datetime(2026, 8, 10, tzinfo=timezone.utc)


def ready_task(storage: SqlTaskStore, task_id: str = "task-100-contended") -> Task:
    return storage.save_task(
        Task(
            id=task_id,
            title="Contended task",
            created=NOW,
            updated=NOW,
            lifecycle=Lifecycle.READY,
            ball=Ball.AGENT,
            ball_reason=BallReason.AVAILABLE,
            priority=Priority.HIGH,
            queue_position=100,
            category="infrastructure",
            spec=Spec(
                summary="Two agents will want this.",
                description="Two agents will want this.",
            ),
        )
    )


class TestTheRace:
    def test_only_one_of_many_racing_agents_wins(self, tmp_path: Path) -> None:
        """The bug, reproduced as a race and then closed.

        Before task-055 this was load -> check -> save with no lock, so every agent
        read the task as ready and every agent wrote itself in as owner. The last write
        won silently and the other agents believed they held a task they did not.
        """
        storage = task_store(tmp_path)
        task = ready_task(storage)
        manager = TaskManager(storage)

        winners: List[str] = []
        losers: List[str] = []
        errors: List[BaseException] = []
        barrier = threading.Barrier(8)
        lock = threading.Lock()

        def claim(agent: str) -> None:
            barrier.wait()  # start all eight at the same instant
            try:
                manager.claim_task(task.id, agent=agent)
                with lock:
                    winners.append(agent)
            except ValueError:
                with lock:
                    losers.append(agent)
            except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                # A claimant that neither won nor was told why is the failure this test
                # exists for: it would otherwise read as a missing loser and look like
                # a flaky count.
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=claim, args=(f"agent-{i}",)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert errors == [], f"claimants failed instead of being refused: {errors}"
        assert len(winners) == 1, f"expected exactly one winner, got {winners}"
        assert len(losers) == 7
        # And the file agrees with whoever won, rather than with the last writer.
        reloaded = storage.load_task(task.id)
        assert reloaded is not None
        assert reloaded.assignment.owner == winners[0]

    def test_the_loser_is_told_why(self, tmp_path: Path) -> None:
        storage = task_store(tmp_path)
        task = ready_task(storage)
        manager = TaskManager(storage)
        manager.claim_task(task.id, agent="claude")

        with pytest.raises(ValueError, match="not available to claim"):
            manager.claim_task(task.id, agent="codex")

    def test_the_refusal_names_the_current_owner(self, tmp_path: Path) -> None:
        storage = task_store(tmp_path)
        task = ready_task(storage)
        manager = TaskManager(storage)
        manager.claim_task(task.id, agent="claude")

        with pytest.raises(ValueError, match="owned by claude"):
            manager.claim_task(task.id, agent="codex")

    def test_concurrent_status_updates_do_not_lose_entries(self, tmp_path: Path) -> None:
        """Lost updates, the other half of the same bug.

        Each writer appends one status update. Without a lock spanning read and write,
        concurrent appends overwrite each other and entries simply disappear.
        """
        storage = task_store(tmp_path)
        task = ready_task(storage)
        manager = TaskManager(storage)
        barrier = threading.Barrier(6)

        def append(index: int) -> None:
            barrier.wait()
            manager.add_progress_update(task.id, author=f"agent-{index}", summary=f"update {index}")

        threads = [threading.Thread(target=append, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        reloaded = storage.load_task(task.id)
        assert reloaded is not None
        bodies = {entry.body for entry in reloaded.log}
        assert len(bodies) == 6, f"entries were lost: {sorted(str(b) for b in bodies)}"
        # And the append-only integrity rules held under contention.
        assert [entry.id for entry in reloaded.log] == sorted(entry.id for entry in reloaded.log)


class TestMutateUnderContention:
    """What the advisory lock was for, over the store that replaced it."""

    def test_mutate_refusing_leaves_the_record_untouched(self, tmp_path: Path) -> None:
        storage = task_store(tmp_path)
        task = ready_task(storage, "task-600-noop")
        before = storage.canonical_bytes(task)

        returned = storage.mutate_task(task.id, lambda _t: None)

        assert returned.id == task.id
        reloaded = storage.load_task(task.id)
        assert reloaded is not None
        assert storage.canonical_bytes(reloaded) == before

    def test_mutating_a_missing_task_raises(self, tmp_path: Path) -> None:
        storage = task_store(tmp_path)

        with pytest.raises(ValueError, match="not found"):
            storage.mutate_task("task-999-gone", lambda t: t)
