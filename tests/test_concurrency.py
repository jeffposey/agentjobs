"""The double-claim race, and the lock that closes it.

The important test here is test_only_one_of_many_racing_agents_wins. It drives real
threads at a real file, and it fails against the pre-task-055 implementation -- which
is the only reason to trust it.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import List
from unittest.mock import patch

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

    def test_a_delete_pending_lock_is_contention_not_a_crash(self, tmp_path: Path) -> None:
        """Windows reports a lock mid-release as PermissionError, not FileExistsError.

        A file whose delete has not finished returns ERROR_ACCESS_DENIED on open, which
        Python raises as PermissionError (EACCES, not EEXIST). The retry loop originally
        matched only FileExistsError, so a losing claimant crashed with "Permission
        denied" instead of being told the task was taken -- about one attempt in forty
        under eight-way contention, which is why it survived a serial test suite.

        Simulated rather than raced, so the regression is deterministic: the first open
        fails the way Windows fails, and the lock must still be acquired on retry.
        """
        storage = TaskStorage(tmp_path)
        ready_task(storage, "task-700-pending")
        real_open = os.open
        calls = {"n": 0}

        def flaky_open(path, flags, *args, **kwargs):
            if str(path).endswith("task-700-pending.lock") and flags & os.O_EXCL:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise PermissionError(13, "Permission denied", str(path))
            return real_open(path, flags, *args, **kwargs)

        with patch("agentjobs.storage.os.open", side_effect=flaky_open):
            with storage.locked("task-700-pending", timeout=2.0):
                pass

        assert calls["n"] >= 2, "the delete-pending failure should have been retried"

    def test_an_unwritable_directory_still_times_out_rather_than_hanging(
        self, tmp_path: Path
    ) -> None:
        """The cost of retrying EACCES: a real permissions fault waits for the timeout.

        It must still end, and the message must name the possibility, or the trade is a
        hang instead of an error.
        """
        storage = TaskStorage(tmp_path)
        ready_task(storage, "task-701-denied")

        def always_denied(path, flags, *args, **kwargs):
            raise PermissionError(13, "Permission denied", str(path))

        with patch("agentjobs.storage.os.open", side_effect=always_denied):
            with pytest.raises(TaskLockTimeout, match="not writable"):
                with storage.locked("task-701-denied", timeout=0.05):
                    pass  # pragma: no cover

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
