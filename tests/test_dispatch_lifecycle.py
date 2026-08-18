"""Cancellation, crash recovery, re-attachment, reaping and lock contention.

The through-line: **a run must never end in silence.** Every path out of a live run --
cancelled, killed, crashed, orphaned by a restart -- has to leave a terminal entry on the
task, because a run that stopped without saying so is indistinguishable from one still
going, and that is the failure this whole subsystem exists to prevent.

Batch and session are opposites in reconciliation, deliberately, and both directions are
tested here rather than trusted to the docstring that explains them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Dict, List

import pytest
import yaml

from agentjobs.dispatch.config import sentinel_path
from agentjobs.dispatch.ledger import (
    DispatchLedger,
    LedgerError,
    RunLockTimeout,
    acquire_run_lock,
    find_run,
    list_runs,
    live_runs,
    read_run,
    write_status,
)
from agentjobs.dispatch.runner import RunDirectory
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Ball, Lifecycle, LogEntryType
from agentjobs.projects import Project, ProjectRegistry
from agentjobs.storage import TaskStorage

PROJECT_CONFIG = {
    "project_name": "Sandbox",
    "tasks_directory": "tasks",
    "actors": [
        {"name": "Jeff Posey", "kind": "human"},
        {"name": "claude", "kind": "agent"},
    ],
    "default_user": "Jeff Posey",
}


# ----- fixtures ---------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path) -> Path:
    machine = tmp_path / "home"
    machine.mkdir()
    return machine


@pytest.fixture
def project(tmp_path: Path, home: Path) -> Project:
    """A registered project, so the ledger can resolve a run back to its task file."""
    root = tmp_path / "proj"
    (root / ".agentjobs").mkdir(parents=True)
    (root / ".agentjobs" / "config.yaml").write_text(
        yaml.safe_dump(PROJECT_CONFIG), encoding="utf-8"
    )
    (root / "tasks").mkdir()
    ProjectRegistry(home=home).add(root, project_id="sandbox")
    return Project(id="sandbox", name="Sandbox", root=root)


@pytest.fixture
def manager(project: Project) -> TaskManager:
    return TaskManager(TaskStorage(project.root / "tasks"))


@pytest.fixture
def task(manager: TaskManager):
    created = manager.create_task(
        title="Dispatchable",
        category="general",
        summary="s",
        description="d",
        lifecycle=Lifecycle.READY,
        actor="Jeff Posey",
    )
    return manager.claim_task(created.id, agent="claude")


def seed_run(
    home: Path,
    task_id: str,
    *,
    run_id: str = "run_test0001",
    mode: str = "batch",
    status: str = "running",
    session_id: str | None = None,
    pid: int | None = None,
    started_at: str = "2026-08-18T08:00:00+00:00",
) -> RunDirectory:
    """A run directory in whatever state the test needs, without spawning anything."""
    meta: Dict[str, object] = {
        "run_id": run_id,
        "task_id": task_id,
        "project_id": "sandbox",
        "mode": mode,
        "posture": "supervised",
        "status": status,
        "started_at": started_at,
        "caused_by": 1,
        "argv": ["fake", "--bg"],
    }
    if session_id:
        meta["session_id"] = session_id
    if pid is not None:
        meta["pid"] = pid
    return RunDirectory.create(home, run_id, meta)


def dispatch_entry(manager: TaskManager, task_id: str, run_id: str) -> int:
    """Write the dispatch entry a terminal result will thread back to."""
    from agentjobs.models_v2 import DispatchMode, DispatchPosture, DispatchTrigger

    updated = manager.record_dispatch(
        task_id,
        actor="Jeff Posey",
        run_id=run_id,
        agent="claude",
        runner="claude",
        mode=DispatchMode.SESSION,
        posture=DispatchPosture.SUPERVISED,
        trigger=DispatchTrigger.MANUAL,
        caused_by=1,
        argv=["claude", "--bg"],
        cwd="C:/proj",
        git_head="abc1234",
    )
    return updated.log[-1].id


FAKE_SESSION_CLI = """
import json, sys, pathlib

sys.stdout.reconfigure(encoding="utf-8")
state = pathlib.Path(__file__).with_name("sessions.json")
argv = sys.argv[1:]

if argv and argv[0] == "agents":
    print(json.dumps(json.loads(state.read_text())))
    raise SystemExit(0)

if argv and argv[0] == "stop":
    rows = json.loads(state.read_text())
    state.write_text(json.dumps([r for r in rows if r["id"] != argv[1]]))
    print("stopped")
    raise SystemExit(0)

if argv and argv[0] == "rm":
    guard = pathlib.Path(__file__).with_name("dirty")
    if guard.exists():
        print("worktree kept: it holds uncommitted changes", file=sys.stderr)
        raise SystemExit(1)
    print("removed")
    raise SystemExit(0)

raise SystemExit(0)
"""


@pytest.fixture
def fake_session_cli(tmp_path: Path) -> Path:
    script = tmp_path / "fakesession.py"
    script.write_text(textwrap.dedent(FAKE_SESSION_CLI), encoding="utf-8")
    (script.parent / "sessions.json").write_text("[]", encoding="utf-8")
    return script


def ledger_with(home: Path, fake_session_cli: Path) -> DispatchLedger:
    return DispatchLedger(
        home,
        registry=ProjectRegistry(home=home),
        session_command=[sys.executable, str(fake_session_cli)],
    )


def set_sessions(fake_session_cli: Path, rows: List[dict]) -> None:
    (fake_session_cli.parent / "sessions.json").write_text(json.dumps(rows), encoding="utf-8")


def results_on(manager: TaskManager, task_id: str) -> list:
    task = manager.get_task(task_id)
    assert task is not None
    return [e for e in task.log if e.type is LogEntryType.DISPATCH_RESULT]


# ----- the ledger reads ------------------------------------------------------


class TestStatus:
    def test_a_run_reports_everything_status_needs(self, home: Path, task) -> None:
        seed_run(home, task.id, mode="session", session_id="b55b35ad", pid=4242)

        record = list_runs(home)[0]

        assert record.run_id == "run_test0001"
        assert record.task_id == task.id
        assert record.mode == "session"
        assert record.session_id == "b55b35ad"
        assert record.pid == 4242
        assert record.elapsed_seconds() is not None
        assert record.is_live

    def test_terminal_runs_are_listed_but_not_live(self, home: Path, task) -> None:
        seed_run(home, task.id, run_id="run_a", status="finished")
        seed_run(home, task.id, run_id="run_b", status="running")

        assert {r.run_id for r in list_runs(home)} == {"run_a", "run_b"}
        assert [r.run_id for r in live_runs(home)] == ["run_b"]

    def test_an_unreadable_run_counts_as_live_rather_than_vanishing(self, home: Path) -> None:
        """It cannot be shown to have ended, and assuming it did would hide a crash."""
        directory = home / "runs" / "run_broken"
        directory.mkdir(parents=True)
        (directory / "meta.yaml").write_text("{{{ not yaml", encoding="utf-8")

        record = read_run(directory)

        assert record.is_live
        assert record.run_id == "run_broken"

    def test_the_lock_directory_is_not_mistaken_for_a_run(self, home: Path, task) -> None:
        acquire_run_lock(home, task.id).release()
        seed_run(home, task.id)

        assert [r.run_id for r in list_runs(home)] == ["run_test0001"]

    def test_an_unknown_run_id_names_what_is_there_instead(self, home: Path, task) -> None:
        seed_run(home, task.id, run_id="run_real")

        with pytest.raises(LedgerError) as caught:
            find_run(home, "run_imaginary")
        assert "run_real" in str(caught.value)


# ----- the run lock -----------------------------------------------------------


class TestRunLock:
    def test_two_runs_cannot_both_hold_one_task(self, home: Path) -> None:
        first = acquire_run_lock(home, "task-001", run_id="run_a")

        with pytest.raises(RunLockTimeout):
            acquire_run_lock(home, "task-001", run_id="run_b", timeout=0.2)

        first.release()

    def test_releasing_lets_the_next_run_take_it(self, home: Path) -> None:
        acquire_run_lock(home, "task-001").release()

        second = acquire_run_lock(home, "task-001", timeout=0.2)

        assert second.path.exists()
        second.release()

    def test_different_tasks_never_contend(self, home: Path) -> None:
        first = acquire_run_lock(home, "task-001")
        second = acquire_run_lock(home, "task-002", timeout=0.2)

        first.release()
        second.release()

    def test_a_stale_lock_times_out_naming_the_file_rather_than_hanging(self, home: Path) -> None:
        """A hang tells you nothing; a named file tells you what to delete."""
        stale = acquire_run_lock(home, "task-001", run_id="run_dead")
        os.close(stale.handle)  # the process died without releasing

        started = time.monotonic()
        with pytest.raises(RunLockTimeout) as caught:
            acquire_run_lock(home, "task-001", timeout=0.3)
        elapsed = time.monotonic() - started

        assert elapsed < 5, "a stale lock must time out, not block"
        assert "task-001.lock" in str(caught.value)
        assert "run_dead" in str(caught.value)
        stale.path.unlink()

    def test_releasing_twice_is_safe(self, home: Path) -> None:
        lock = acquire_run_lock(home, "task-001")
        lock.release()
        lock.release()

    def test_only_one_of_many_threads_gets_it(self, home: Path) -> None:
        winners: List[object] = []

        def attempt() -> None:
            try:
                winners.append(acquire_run_lock(home, "task-001", timeout=0.2))
            except RunLockTimeout:
                pass

        threads = [threading.Thread(target=attempt) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert len(winners) == 1
        winners[0].release()  # type: ignore[attr-defined]


# ----- cancellation -----------------------------------------------------------


class TestCancel:
    def test_cancelling_a_session_delegates_to_stop_and_records_the_outcome(
        self, home: Path, task, manager: TaskManager, fake_session_cli: Path
    ) -> None:
        entry_id = dispatch_entry(manager, task.id, "run_test0001")
        seed_run(home, task.id, mode="session", session_id="b55b35ad")
        set_sessions(fake_session_cli, [{"id": "b55b35ad", "status": "busy", "state": "working"}])
        ledger = ledger_with(home, fake_session_cli)

        result = ledger.cancel("run_test0001")

        assert result.stopped
        assert json.loads((fake_session_cli.parent / "sessions.json").read_text()) == []
        entries = results_on(manager, task.id)
        assert len(entries) == 1
        assert entries[0].data["outcome"] == "cancelled"
        assert entries[0].re == entry_id

    def test_cancelling_a_session_never_touches_a_process_group(
        self, home: Path, task, manager: TaskManager, fake_session_cli: Path
    ) -> None:
        """`claude stop` is the interface; a pid would desync the manager's own ledger."""
        dispatch_entry(manager, task.id, "run_test0001")
        seed_run(home, task.id, mode="session", session_id="b55b35ad", pid=999999)
        set_sessions(fake_session_cli, [{"id": "b55b35ad", "status": "busy", "state": "working"}])
        ledger = ledger_with(home, fake_session_cli)

        killed: List[int] = []
        import agentjobs.dispatch.runner as runner_module

        original = runner_module._kill_tree
        runner_module._kill_tree = lambda pid: killed.append(pid)  # type: ignore[assignment]
        try:
            ledger.cancel("run_test0001")
        finally:
            runner_module._kill_tree = original  # type: ignore[assignment]

        assert killed == [], "a session cancel must not kill a process tree"

    def test_cancelling_a_batch_run_kills_the_tree_and_records_the_outcome(
        self, home: Path, task, manager: TaskManager, tmp_path: Path, fake_session_cli: Path
    ) -> None:
        script = tmp_path / "sleeper.py"
        script.write_text("import time; time.sleep(600)\n", encoding="utf-8")
        process = subprocess.Popen([sys.executable, str(script)])
        dispatch_entry(manager, task.id, "run_test0001")
        seed_run(home, task.id, mode="batch", pid=process.pid)
        ledger = ledger_with(home, fake_session_cli)

        result = ledger.cancel("run_test0001")

        assert result.stopped
        assert process.wait(timeout=30) is not None
        entries = results_on(manager, task.id)
        assert len(entries) == 1
        assert entries[0].data["outcome"] == "cancelled"

    def test_cancelling_a_finished_run_changes_nothing(
        self, home: Path, task, manager: TaskManager, fake_session_cli: Path
    ) -> None:
        seed_run(home, task.id, status="finished")

        result = ledger_with(home, fake_session_cli).cancel("run_test0001")

        assert not result.stopped
        assert results_on(manager, task.id) == []

    def test_a_cancelled_run_hands_the_ball_to_a_human(
        self, home: Path, task, manager: TaskManager, fake_session_cli: Path
    ) -> None:
        dispatch_entry(manager, task.id, "run_test0001")
        seed_run(home, task.id, mode="session", session_id="b55b35ad")
        set_sessions(fake_session_cli, [{"id": "b55b35ad", "status": "busy", "state": "working"}])

        ledger_with(home, fake_session_cli).cancel("run_test0001")

        after = manager.get_task(task.id)
        assert after is not None and after.ball is Ball.HUMAN


class TestStopEverything:
    def test_it_writes_the_sentinel_and_stops_every_live_run(
        self, home: Path, task, manager: TaskManager, fake_session_cli: Path
    ) -> None:
        dispatch_entry(manager, task.id, "run_a")
        seed_run(home, task.id, run_id="run_a", mode="session", session_id="s1")
        seed_run(home, task.id, run_id="run_done", status="finished")
        set_sessions(fake_session_cli, [{"id": "s1", "status": "busy", "state": "working"}])

        results = ledger_with(home, fake_session_cli).stop_everything()

        assert sentinel_path(home).exists()
        assert [r.run_id for r in results] == ["run_a"]
        assert live_runs(home) == []

    def test_the_sentinel_is_written_before_anything_is_stopped(
        self, home: Path, task, fake_session_cli: Path
    ) -> None:
        """Stopping can take a while; nothing new may start in the meantime."""
        seen: List[bool] = []
        ledger = ledger_with(home, fake_session_cli)
        seed_run(home, task.id, mode="session", session_id="s1")
        set_sessions(fake_session_cli, [{"id": "s1", "status": "busy", "state": "working"}])

        original = ledger._stop

        def observe(record):  # type: ignore[no-untyped-def]
            seen.append(sentinel_path(home).exists())
            return original(record)

        ledger._stop = observe  # type: ignore[assignment]
        ledger.stop_everything()

        assert seen == [True]

    def test_stopping_with_nothing_running_still_arms_the_sentinel(
        self, home: Path, fake_session_cli: Path
    ) -> None:
        results = ledger_with(home, fake_session_cli).stop_everything()

        assert results == []
        assert sentinel_path(home).exists()


# ----- reconciliation ---------------------------------------------------------


class TestReconcile:
    def test_a_batch_run_that_lost_its_supervisor_is_marked_interrupted(
        self, home: Path, task, manager: TaskManager, fake_session_cli: Path
    ) -> None:
        """Simulates a hard crash: the run is live on disk and nothing is watching it."""
        dispatch_entry(manager, task.id, "run_test0001")
        seed_run(home, task.id, mode="batch", pid=None)

        results = ledger_with(home, fake_session_cli).reconcile()

        assert results[0].stopped
        entries = results_on(manager, task.id)
        assert len(entries) == 1
        assert entries[0].data["outcome"] == "interrupted"
        after = manager.get_task(task.id)
        assert after is not None and after.ball is Ball.HUMAN

    def test_a_live_session_is_re_attached_not_killed(
        self, home: Path, task, manager: TaskManager, fake_session_cli: Path
    ) -> None:
        """Killing it because the server restarted would destroy real work for no gain."""
        dispatch_entry(manager, task.id, "run_test0001")
        seed_run(home, task.id, mode="session", session_id="s1")
        set_sessions(fake_session_cli, [{"id": "s1", "status": "busy", "state": "working"}])

        results = ledger_with(home, fake_session_cli).reconcile()

        assert not results[0].stopped
        assert "re-attached" in results[0].detail
        assert results_on(manager, task.id) == []
        assert live_runs(home)[0].run_id == "run_test0001"
        assert json.loads((fake_session_cli.parent / "sessions.json").read_text())

    def test_a_session_the_manager_has_forgotten_gets_a_terminal_entry(
        self, home: Path, task, manager: TaskManager, fake_session_cli: Path
    ) -> None:
        dispatch_entry(manager, task.id, "run_test0001")
        seed_run(home, task.id, mode="session", session_id="s1")
        set_sessions(fake_session_cli, [])

        results = ledger_with(home, fake_session_cli).reconcile()

        assert results[0].stopped
        assert results_on(manager, task.id)[0].data["outcome"] == "interrupted"

    def test_an_unreadable_session_ledger_leaves_sessions_alone(
        self, home: Path, task, manager: TaskManager, tmp_path: Path
    ) -> None:
        """Not being able to look must not be reported as "the work is dead"."""
        dispatch_entry(manager, task.id, "run_test0001")
        seed_run(home, task.id, mode="session", session_id="s1")
        ledger = DispatchLedger(
            home,
            registry=ProjectRegistry(home=home),
            session_command=["definitely-not-a-real-binary"],
        )

        results = ledger.reconcile()

        assert not results[0].stopped
        assert results_on(manager, task.id) == []

    def test_reconciling_twice_does_not_write_two_terminal_entries(
        self, home: Path, task, manager: TaskManager, fake_session_cli: Path
    ) -> None:
        dispatch_entry(manager, task.id, "run_test0001")
        seed_run(home, task.id, mode="batch")
        ledger = ledger_with(home, fake_session_cli)

        ledger.reconcile()
        ledger.reconcile()

        assert len(results_on(manager, task.id)) == 1


# ----- reaping ----------------------------------------------------------------


class TestReap:
    def test_a_finished_session_is_removed(self, home: Path, task, fake_session_cli: Path) -> None:
        seed_run(home, task.id, mode="session", session_id="s1", status="finished")

        results = ledger_with(home, fake_session_cli).reap_finished()

        assert [r.stopped for r in results] == [True]
        assert read_run(home / "runs" / "run_test0001").path.exists()

    def test_a_reap_blocked_by_uncommitted_changes_says_so(
        self, home: Path, task, fake_session_cli: Path
    ) -> None:
        """That refusal means a run produced work nobody has looked at. Surface it."""
        (fake_session_cli.parent / "dirty").write_text("", encoding="utf-8")
        seed_run(home, task.id, mode="session", session_id="s1", status="finished")

        results = ledger_with(home, fake_session_cli).reap_finished()

        assert not results[0].stopped
        assert "uncommitted" in results[0].detail
        meta = yaml.safe_load((home / "runs" / "run_test0001" / "meta.yaml").read_text())
        assert "uncommitted" in meta["reap_blocked"]

    def test_a_reaped_session_is_not_reaped_again(
        self, home: Path, task, fake_session_cli: Path
    ) -> None:
        seed_run(home, task.id, mode="session", session_id="s1", status="finished")
        ledger = ledger_with(home, fake_session_cli)

        ledger.reap_finished()
        second = ledger.reap_finished()

        assert second == []

    def test_batch_runs_are_never_reaped(self, home: Path, task, fake_session_cli: Path) -> None:
        """There is no session to remove; the process is already gone."""
        seed_run(home, task.id, mode="batch", status="finished")

        assert ledger_with(home, fake_session_cli).reap_finished() == []

    def test_a_live_session_is_never_reaped(self, home: Path, task, fake_session_cli: Path) -> None:
        seed_run(home, task.id, mode="session", session_id="s1", status="running")

        assert ledger_with(home, fake_session_cli).reap_finished() == []


# ----- a run that cannot be attributed ----------------------------------------


class TestUnattributableRun:
    def test_an_unresolvable_project_still_stops_counting_as_live(
        self, home: Path, fake_session_cli: Path
    ) -> None:
        """The task record cannot be written, so the run's own meta records why."""
        RunDirectory.create(
            home,
            "run_orphan",
            {
                "run_id": "run_orphan",
                "task_id": "task-999",
                "project_id": "vanished",
                "mode": "batch",
                "status": "running",
                "started_at": "2026-08-18T08:00:00+00:00",
            },
        )

        ledger_with(home, fake_session_cli).reconcile()

        assert live_runs(home) == []
        meta = yaml.safe_load((home / "runs" / "run_orphan" / "meta.yaml").read_text())
        assert "vanished" in str(meta.get("unattributed", "")) or meta["outcome"] == "interrupted"


def test_write_status_merges_rather_than_replaces(home: Path) -> None:
    directory = RunDirectory.create(home, "run_x", {"run_id": "run_x", "status": "running"})

    write_status(read_run(directory.path), outcome="cancelled")

    meta = yaml.safe_load((directory.path / "meta.yaml").read_text())
    assert meta["run_id"] == "run_x"
    assert meta["outcome"] == "cancelled"
