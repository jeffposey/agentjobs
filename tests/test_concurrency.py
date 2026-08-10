"""The double-claim race, and the lock that closes it.

The important test here is test_only_one_of_many_racing_agents_wins. It drives real
threads at a real file, and it fails against the pre-task-055 implementation -- which
is the only reason to trust it.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import List

import pytest

from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Ball, BallReason, Lifecycle, Priority, Spec, Task
from agentjobs.storage import TaskLockTimeout, TaskStorage

NOW = datetime(2026, 8, 10, tzinfo=timezone.utc)


def ready_task(storage: TaskStorage, task_id: str = "task-100-contended") -> Task:
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
        storage = TaskStorage(tmp_path)
        task = ready_task(storage)
        manager = TaskManager(storage)

        winners: List[str] = []
        losers: List[str] = []
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

        threads = [threading.Thread(target=claim, args=(f"agent-{i}",)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert len(winners) == 1, f"expected exactly one winner, got {winners}"
        assert len(losers) == 7
        # And the file agrees with whoever won, rather than with the last writer.
        reloaded = storage.load_task(task.id)
        assert reloaded is not None
        assert reloaded.assignment.owner == winners[0]

    def test_the_loser_is_told_why(self, tmp_path: Path) -> None:
        storage = TaskStorage(tmp_path)
        task = ready_task(storage)
        manager = TaskManager(storage)
        manager.claim_task(task.id, agent="claude")

        with pytest.raises(ValueError, match="not available to claim"):
            manager.claim_task(task.id, agent="codex")

    def test_the_refusal_names_the_current_owner(self, tmp_path: Path) -> None:
        storage = TaskStorage(tmp_path)
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
        storage = TaskStorage(tmp_path)
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


class TestTheLockItself:
    def test_it_is_exclusive(self, tmp_path: Path) -> None:
        storage = TaskStorage(tmp_path)
        ready_task(storage, "task-200-lock")

        with storage.locked("task-200-lock"):
            with pytest.raises(TaskLockTimeout):
                with storage.locked("task-200-lock", timeout=0.05):
                    pass  # pragma: no cover - the point is that we never get here

    def test_it_is_released_even_when_the_block_raises(self, tmp_path: Path) -> None:
        storage = TaskStorage(tmp_path)
        ready_task(storage, "task-201-lock")

        with pytest.raises(RuntimeError):
            with storage.locked("task-201-lock"):
                raise RuntimeError("boom")

        # If the lock leaked, this would time out instead of returning.
        with storage.locked("task-201-lock", timeout=0.5):
            pass

    def test_different_tasks_do_not_contend(self, tmp_path: Path) -> None:
        storage = TaskStorage(tmp_path)
        ready_task(storage, "task-300-a")
        ready_task(storage, "task-301-b")

        with storage.locked("task-300-a"):
            with storage.locked("task-301-b", timeout=0.5):
                pass

    def test_the_timeout_message_explains_the_stale_lock_case(self, tmp_path: Path) -> None:
        storage = TaskStorage(tmp_path)
        ready_task(storage, "task-400-stale")

        with storage.locked("task-400-stale"):
            with pytest.raises(TaskLockTimeout, match="left the lock behind"):
                with storage.locked("task-400-stale", timeout=0.05):
                    pass  # pragma: no cover

    def test_lock_files_are_not_mistaken_for_tasks(self, tmp_path: Path) -> None:
        # Lock files sit beside task files in the same directory. If the glob picked
        # them up they would show as broken tasks in every listing.
        storage = TaskStorage(tmp_path)
        ready_task(storage, "task-500-glob")

        with storage.locked("task-500-glob"):
            result = storage.load_all()

        assert [t.id for t in result.tasks] == ["task-500-glob"]
        assert result.errors == []

    def test_mutate_refusing_leaves_the_file_untouched(self, tmp_path: Path) -> None:
        storage = TaskStorage(tmp_path)
        task = ready_task(storage, "task-600-noop")
        before = (tmp_path / "task-600-noop.yaml").read_text(encoding="utf-8")

        returned = storage.mutate_task(task.id, lambda _t: None)

        assert returned.id == task.id
        assert (tmp_path / "task-600-noop.yaml").read_text(encoding="utf-8") == before

    def test_mutating_a_missing_task_raises(self, tmp_path: Path) -> None:
        storage = TaskStorage(tmp_path)

        with pytest.raises(ValueError, match="not found"):
            storage.mutate_task("task-999-gone", lambda t: t)
