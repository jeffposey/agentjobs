"""Tests for the thing that *calls* the session decision, not the decision itself.

`poll_session` was complete, correct, unit-tested and never invoked. Its tests passed
for as long as the defect existed, because they called it themselves. So every test here
is about the calling: does a live session run get found on disk, resolved back to its
project, and handed over -- and does anything actually turn the crank on an interval.

The fake CLI is the same seam `test_dispatch_runner.py` uses: session mode is defined
operationally as "a runner whose executable answers `agents --json`", so a fake that
answers it exercises the real path.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import List

import pytest
import yaml

from agentjobs.dispatch.ledger import acquire_run_lock
from agentjobs.dispatch.poller import (
    SESSION_POLL_SECONDS,
    poll_live_sessions,
    poll_sessions_forever,
)
from agentjobs.dispatch.runner import TRANSCRIPT_FILENAME, DispatchRunner, SessionPhase
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Ball, BallReason, Lifecycle, LogEntryType
from agentjobs.projects import ProjectRegistry
from agentjobs.storage import TaskStorage

from test_dispatch_runner import FAKE_CLI, write_script

# ----- a machine with dispatch configured -------------------------------------


def _dispatch_yaml(home: Path, fake_cli: Path, *, project_id: str = "sandbox") -> None:
    """The machine-local config `assert_dispatch_permitted` reads. Written, not faked.

    The poller resolves its own runner from this file rather than being handed one, which
    is the part of it worth testing: a scheduler that could only poll runs it started
    itself would never settle a run left behind by a restart.
    """
    (home / "dispatch.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "enabled": True,
                "runners": {
                    "fake": {
                        "mode": "session",
                        "argv": [sys.executable, str(fake_cli), "--bg", "{prompt}"],
                    }
                },
                "projects": {
                    project_id: {
                        "enabled": True,
                        "runner": "fake",
                        "posture": "autonomous",
                        "require_clean_tree": False,
                    }
                },
                "limits": {"session_stale_seconds": 1},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


@pytest.fixture
def machine(tmp_path: Path):
    """A registered project, a configured dispatch home, and a fake session CLI."""
    home = tmp_path / "home"
    root = tmp_path / "project"
    (root / ".agentjobs").mkdir(parents=True)
    (root / "tasks").mkdir()
    home.mkdir()
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump({"project_name": "Sandbox", "tasks_directory": "tasks"}), encoding="utf-8"
    )
    fake_cli = write_script(tmp_path / "fakecli.py", FAKE_CLI)
    _dispatch_yaml(home, fake_cli)
    ProjectRegistry(home=home).add(root, project_id="sandbox")
    manager = TaskManager(TaskStorage(root / "tasks"))
    return home, root, manager, fake_cli


def _dispatched_task(manager: TaskManager) -> str:
    created = manager.create_task(
        title="Dispatchable",
        category="infrastructure",
        summary="A task to dispatch.",
        description="Do the thing.",
        lifecycle=Lifecycle.READY,
    )
    manager.claim_task(created.id, agent="claude")
    return created.id


def _start_session(machine) -> str:
    """Start a run the way dispatch does, so its meta.yaml is the real article."""
    home, root, manager, _ = machine
    from agentjobs.dispatch.config import assert_dispatch_permitted

    task_id = _dispatched_task(manager)
    runner = DispatchRunner(
        manager=manager,
        resolution=assert_dispatch_permitted("sandbox", home),
        project_root=root,
        home=home,
    )
    handle = runner.start(manager.get_task(task_id), actor="Jeff Posey", caused_by=1)
    return handle.run_id


def _set_ledger(fake_cli: Path, rows: List[dict]) -> None:
    (fake_cli.parent / "ledger.json").write_text(json.dumps(rows), encoding="utf-8")


def _results(home: Path, run_id: str):
    return {result.run_id: result for result in poll_live_sessions(home)}[run_id]


def _run_meta(home: Path, run_id: str) -> dict:
    meta = yaml.safe_load((home / "runs" / run_id / "meta.yaml").read_text(encoding="utf-8"))
    assert isinstance(meta, dict)
    return meta


# ----- the gap this task was filed for ----------------------------------------


class TestPollingFindsAndSettlesRuns:
    def test_a_finished_session_is_settled_without_anyone_calling_poll_session(
        self, machine
    ) -> None:
        """The whole defect: nothing turned the crank, so a finished run stayed `running`."""
        home, _, manager, fake_cli = machine
        run_id = _start_session(machine)
        task_id = _run_meta(home, run_id)["task_id"]
        # The session hands off and then ends, which is a clean finish.
        manager.handoff(
            task_id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Please look at this.",
        )
        _set_ledger(fake_cli, [{"id": "b55b35ad", "status": "idle", "state": "done"}])

        result = _results(home, run_id)

        assert result.phase is SessionPhase.FINISHED
        assert _run_meta(home, run_id)["status"] == "finished"
        after = manager.get_task(task_id)
        assert after is not None
        assert [entry for entry in after.log if entry.type is LogEntryType.DISPATCH_RESULT]

    def test_a_still_running_session_is_left_exactly_as_it_was(self, machine) -> None:
        home, _, manager, fake_cli = machine
        run_id = _start_session(machine)
        _set_ledger(fake_cli, [{"id": "b55b35ad", "status": "busy", "state": "working"}])

        result = _results(home, run_id)

        assert result.phase is SessionPhase.RUNNING
        assert result.acted is False
        assert _run_meta(home, run_id)["status"] == "running"

    def test_a_parked_session_becomes_a_handoff_from_the_poller_alone(self, machine) -> None:
        home, _, manager, fake_cli = machine
        run_id = _start_session(machine)
        task_id = _run_meta(home, run_id)["task_id"]
        _set_ledger(fake_cli, [{"id": "b55b35ad", "status": "waiting", "state": "blocked"}])

        assert _results(home, run_id).phase is SessionPhase.PARKED

        after = manager.get_task(task_id)
        assert after is not None
        assert after.ball is Ball.HUMAN
        assert after.ball_reason is BallReason.INPUT

    def test_a_batch_run_is_not_polled(self, machine) -> None:
        """A batch run has a supervisor thread of its own; polling it would double up."""
        home, _, _, _ = machine
        directory = home / "runs" / "run_batch01"
        directory.mkdir(parents=True)
        (directory / "meta.yaml").write_text(
            yaml.safe_dump(
                {
                    "run_id": "run_batch01",
                    "task_id": "task-001",
                    "project_id": "sandbox",
                    "mode": "batch",
                    "status": "running",
                }
            ),
            encoding="utf-8",
        )

        assert [result.run_id for result in poll_live_sessions(home)] == []


class TestTheLockIsReleasedByWhateverConcludesTheRun:
    """ac-4: the completion path must not depend on an in-memory handle surviving.

    A session's terminal write happens here, in the poller, not in the call that started
    it -- that call returned minutes or hours earlier and took the ``RunLock`` object
    with it. Until task-190 the handle rebuilt here left ``lock`` unset, so the release
    in ``_finish_session`` was a silent no-op and every session run this settled stranded
    its lock on disk. Two of the four locks found on 2026-08-20 were exactly that, held
    by the live server that had started them.
    """

    def _lock_for(self, home: Path, run_id: str):
        task_id = _run_meta(home, run_id)["task_id"]
        lock = acquire_run_lock(home, task_id)
        lock.adopt(run_id)
        return lock

    def test_settling_a_finished_session_releases_its_run_lock(self, machine) -> None:
        home, _, manager, fake_cli = machine
        run_id = _start_session(machine)
        task_id = _run_meta(home, run_id)["task_id"]
        lock = self._lock_for(home, run_id)
        manager.handoff(
            task_id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Please look at this.",
        )
        _set_ledger(fake_cli, [{"id": "b55b35ad", "status": "idle", "state": "done"}])

        assert _results(home, run_id).phase is SessionPhase.FINISHED

        assert not lock.path.exists(), "a settled session must not leave its lock behind"

    def test_a_session_still_running_keeps_its_lock(self, machine) -> None:
        home, _, _, fake_cli = machine
        run_id = _start_session(machine)
        lock = self._lock_for(home, run_id)
        _set_ledger(fake_cli, [{"id": "b55b35ad", "status": "busy", "state": "working"}])

        assert _results(home, run_id).phase is SessionPhase.RUNNING

        assert lock.path.exists()
        lock.release()

    def test_a_session_the_ledger_has_lost_releases_its_lock_too(self, machine) -> None:
        """GONE is a terminal path as much as FINISHED, and it went through the same no-op."""
        home, _, _, fake_cli = machine
        run_id = _start_session(machine)
        lock = self._lock_for(home, run_id)
        _set_ledger(fake_cli, [])

        assert _results(home, run_id).phase is SessionPhase.GONE

        assert not lock.path.exists()


class TestRunsThatCannotBeFollowed:
    def test_a_run_with_no_dispatch_entry_is_skipped_rather_than_misread(self, machine) -> None:
        """Without it `_ball_moved` is False, so a clean finish would be reported as a
        session that stopped without handing off -- alarming, and wrong."""
        home, _, _, fake_cli = machine
        run_id = _start_session(machine)
        meta = _run_meta(home, run_id)
        del meta["dispatch_entry_id"]
        (home / "runs" / run_id / "meta.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")
        _set_ledger(fake_cli, [{"id": "b55b35ad", "status": "idle", "state": "done"}])

        result = _results(home, run_id)

        assert result.phase is None
        assert "dispatch entry" in result.detail
        assert _run_meta(home, run_id)["status"] == "running", "settled on a guess"

    def test_a_project_the_registry_does_not_hold_is_reported_not_dropped(self, machine) -> None:
        """`_local` runs land here. Silence would mean nothing ever concludes them."""
        home, _, _, _ = machine
        run_id = _start_session(machine)
        meta = _run_meta(home, run_id)
        meta["project_id"] = "_local"
        (home / "runs" / run_id / "meta.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")

        result = _results(home, run_id)

        assert result.phase is None
        assert "_local" in result.detail and "registry" in result.detail

    def test_dispatch_being_switched_off_leaves_a_started_run_alone(self, machine) -> None:
        """The gate answers "may a run start", which says nothing about one already going."""
        home, _, _, fake_cli = machine
        run_id = _start_session(machine)
        (home / "DISPATCH_DISABLED").write_text("", encoding="utf-8")
        _set_ledger(fake_cli, [{"id": "b55b35ad", "status": "idle", "state": "done"}])

        result = _results(home, run_id)

        assert result.phase is None
        assert "no longer permitted" in result.detail
        assert _run_meta(home, run_id)["status"] == "running"


class TestTranscriptCapture:
    def test_polling_writes_the_session_transcript_into_the_run_directory(self, machine) -> None:
        """The run directory has to be a complete account of the run on its own: the
        session's own store is not ours, and reaping discards it."""
        home, _, _, fake_cli = machine
        run_id = _start_session(machine)
        _set_ledger(fake_cli, [{"id": "b55b35ad", "status": "busy", "state": "working"}])

        poll_live_sessions(home)

        captured = (home / "runs" / run_id / TRANSCRIPT_FILENAME).read_text(encoding="utf-8")
        assert "Claude needs your permission to run" in captured

    def test_a_finished_run_keeps_the_transcript_it_had_before_it_was_reaped(self, machine) -> None:
        """Reaping deletes the session, so fetching on demand afterwards reads nothing.
        The last capture before settling is what a human comes back to."""
        home, _, manager, fake_cli = machine
        run_id = _start_session(machine)
        task_id = _run_meta(home, run_id)["task_id"]
        manager.handoff(
            task_id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Please look at this.",
        )
        _set_ledger(fake_cli, [{"id": "b55b35ad", "status": "idle", "state": "done"}])

        poll_live_sessions(home)

        assert json.loads((fake_cli.parent / "ledger.json").read_text()) == [], "not reaped"
        captured = (home / "runs" / run_id / TRANSCRIPT_FILENAME).read_text(encoding="utf-8")
        assert "poetry run alembic upgrade head" in captured


# ----- the loop ---------------------------------------------------------------


class TestTheScheduler:
    def test_it_polls_before_it_first_sleeps(self, machine) -> None:
        """A session that ended while AgentJobs was down settles at startup, not an
        interval later. That is what makes a restart clear it."""
        home, _, manager, fake_cli = machine
        run_id = _start_session(machine)
        task_id = _run_meta(home, run_id)["task_id"]
        manager.handoff(
            task_id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Please look at this.",
        )
        _set_ledger(fake_cli, [{"id": "b55b35ad", "status": "idle", "state": "done"}])
        lines: List[str] = []

        async def one_tick() -> None:
            task = asyncio.create_task(
                poll_sessions_forever(home, interval=3600, report=lines.append)
            )
            # Long enough for one poll -- which spawns real processes -- and far short of
            # the interval, so what is asserted is the poll that happened before it.
            await asyncio.sleep(0)
            for _ in range(400):
                if lines:
                    break
                await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(one_tick())

        assert _run_meta(home, run_id)["status"] == "finished"
        assert any(run_id in line and "finished" in line for line in lines)

    def test_it_reports_a_change_once_rather_than_every_tick(self, machine) -> None:
        """A line every ten seconds saying a run is still running buries the lines worth
        reading, which is how a server log stops being read at all."""
        home, _, _, fake_cli = machine
        _start_session(machine)
        _set_ledger(fake_cli, [{"id": "b55b35ad", "status": "busy", "state": "working"}])
        lines: List[str] = []

        async def several_ticks() -> None:
            task = asyncio.create_task(
                poll_sessions_forever(home, interval=0.01, report=lines.append)
            )
            await asyncio.sleep(1.0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(several_ticks())

        assert len(lines) == 1, f"repeated itself: {lines}"

    def test_the_interval_is_stated_and_slow_enough_to_be_free(self) -> None:
        """Stated in code because sc-9 asks for it, and because the browser's tail is
        deliberately no faster than this."""
        assert 5.0 <= SESSION_POLL_SECONDS <= 60.0


class TestTheServerStartsIt:
    def test_serving_turns_the_crank_without_anyone_asking(self, machine, monkeypatch) -> None:
        """The defect in one line: every other test here would pass with the poller
        wired to nothing. This one fails unless the running server starts it."""
        from fastapi.testclient import TestClient

        import agentjobs.dispatch.poller as poller
        from agentjobs.api.dependencies import reset_dependency_cache
        from agentjobs.api.main import app

        home, _, _, _ = machine
        monkeypatch.setenv("AGENTJOBS_HOME", str(home))
        reset_dependency_cache()
        polled: List[Path] = []

        def record(where: Path) -> List[object]:
            polled.append(where)
            return []

        monkeypatch.setattr(poller, "poll_live_sessions", record)

        with TestClient(app) as client:
            for _ in range(200):
                if polled:
                    break
                time.sleep(0.02)
            assert client.get("/health").status_code == 200

        reset_dependency_cache()
        assert polled, "nothing polled live sessions while the server was up"
        assert polled[0] == home
        # And it stops with the server rather than outliving it.
        before = len(polled)
        time.sleep(0.1)
        assert len(polled) == before
