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
from datetime import datetime, timezone
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
    locks_root,
    read_lock_holder,
    read_run,
    release_stale_locks,
    write_status,
)
from agentjobs.dispatch.runner import RunDirectory
from agentjobs.manager import TaskManager
from agentjobs.models_v2 import Ball, Lifecycle, LogEntryType, Outcome
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


@pytest.fixture()
def closed_task(manager: TaskManager, task):
    """A task nothing is coming back for.

    The reaping tests need one because reaping is now conditional on the task: an *open*
    task's newest session is kept so a later dispatch can resume its conversation rather
    than boot a cold agent (task-234). These tests are about the mechanics of removal --
    that it issues `rm`, that a refusal is surfaced and never forced -- so they take a
    task where removal is unambiguously the right thing to do, and the keeping behaviour
    has tests of its own below.
    """
    return manager.close_task(task.id, actor="claude", outcome=Outcome.COMPLETED)


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
    finished_at: str | None = None,
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
    if finished_at is not None:
        meta["finished_at"] = finished_at
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
calls = pathlib.Path(__file__).with_name("calls.json")
argv = sys.argv[1:]

calls.write_text(json.dumps((json.loads(calls.read_text()) if calls.exists() else []) + [argv]))

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


# ----- how long it took -------------------------------------------------------


class TestDuration:
    """A concluded run's duration is a fact about the past and must stop moving.

    It did not: elapsed time was ``now - started_at`` whatever the run's state, so a run
    that took 42 seconds reported 11.6 hours the next morning, and every duration in the
    UI was wrong by an amount that depended on when you looked (task-158).
    """

    def test_a_finished_run_reports_the_same_duration_however_late_you_read_it(
        self, home: Path, task
    ) -> None:
        seed_run(
            home,
            task.id,
            status="finished",
            started_at="2026-08-18T08:00:00+00:00",
            finished_at="2026-08-18T08:00:42+00:00",
        )

        record = list_runs(home)[0]

        assert record.elapsed_seconds() == 42.0
        assert record.elapsed_seconds(now=datetime(2027, 1, 1, tzinfo=timezone.utc)) == 42.0

    def test_a_live_run_still_counts_up(self, home: Path, task) -> None:
        seed_run(home, task.id, status="running", started_at="2026-08-18T08:00:00+00:00")

        record = list_runs(home)[0]
        earlier = record.elapsed_seconds(now=datetime(2026, 8, 18, 8, 0, 30, tzinfo=timezone.utc))
        later = record.elapsed_seconds(now=datetime(2026, 8, 18, 8, 1, 0, tzinfo=timezone.utc))

        assert earlier == 30.0
        assert later == 60.0

    def test_a_concluded_run_with_no_finish_time_reports_unknown(self, home: Path, task) -> None:
        """Runs from before finish times were recorded. Better unknown than invented."""
        seed_run(home, task.id, status="failed", finished_at=None)

        assert list_runs(home)[0].elapsed_seconds() is None

    def test_ending_a_run_stamps_the_finish_time_even_when_the_caller_forgets(
        self, home: Path, task
    ) -> None:
        """The stamp lives in the write, so no path out of a live run can omit it."""
        directory = seed_run(home, task.id, status="running")

        directory.update_meta(status="failed", error="launcher exploded")
        by_update_meta = read_run(directory.path).elapsed_seconds()

        second = seed_run(home, task.id, run_id="run_two")
        write_status(read_run(second.path), status="cancelled")
        by_write_status = read_run(home / "runs" / "run_two").elapsed_seconds()

        assert by_update_meta is not None and by_update_meta > 0
        assert by_write_status is not None and by_write_status > 0

    def test_a_running_run_is_not_stamped(self, home: Path, task) -> None:
        directory = seed_run(home, task.id, status="unknown")

        directory.update_meta(status="running", pid=4242)

        assert "finished_at" not in directory.read_meta()

    def test_the_run_and_the_task_agree_on_how_long_it_took(
        self, home: Path, task, manager: TaskManager, fake_session_cli: Path
    ) -> None:
        """sc-2: one instant, written to both places, so the two records cannot diverge."""
        dispatch_entry(manager, task.id, "run_test0001")
        seed_run(home, task.id, mode="session", session_id="b55b35ad")
        set_sessions(fake_session_cli, [{"id": "b55b35ad", "status": "busy", "state": "working"}])

        ledger_with(home, fake_session_cli).cancel("run_test0001")

        on_the_run = find_run(home, "run_test0001").elapsed_seconds()
        on_the_task = results_on(manager, task.id)[0].data["duration_seconds"]
        assert on_the_run == on_the_task


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

    def test_the_descriptor_is_closed_so_the_file_can_be_deleted(self, home: Path) -> None:
        """The claim is the file's existence; holding the handle open only blocks unlink.

        On Windows an open handle makes ``unlink`` fail with ``Device or resource busy``,
        which is what made a leaked lock undeletable for as long as the server that
        leaked it lived -- and turned the old "delete the file to clear it" advice into
        "restart the thing you are trying to use" (task-190).
        """
        lock = acquire_run_lock(home, "task-001", run_id="run_a")

        lock.path.unlink()  # would raise PermissionError with the descriptor still open

        assert not lock.path.exists()

    def test_a_lock_records_the_run_it_is_held_for(self, home: Path) -> None:
        lock = acquire_run_lock(home, "task-001")
        assert "run=" in lock.path.read_text(encoding="utf-8")

        lock.adopt("run_named")

        holder = read_lock_holder(lock.path)
        assert holder is not None
        assert holder.run_id == "run_named"
        assert holder.pid == os.getpid()
        lock.release()

    def test_a_lock_whose_run_has_ended_is_reclaimed(self, home: Path) -> None:
        """ac-1: a task whose runs are all terminal is dispatchable, lock file or not."""
        seed_run(home, "task-001", run_id="run_over", status="finished")
        leaked = acquire_run_lock(home, "task-001")
        leaked.adopt("run_over")

        taken = acquire_run_lock(home, "task-001", run_id="run_next", timeout=0.5)

        holder = read_lock_holder(taken.path)
        assert holder is not None and holder.run_id == "run_next"
        taken.release()

    def test_a_lock_whose_run_is_live_still_refuses(self, home: Path) -> None:
        """ac-2, and the whole reason this is judged rather than swept.

        Distinguished from the case above by exactly one field -- the run's status -- so
        a change that started clearing live locks fails here rather than in production.
        """
        seed_run(home, "task-001", run_id="run_going", status="running")
        held = acquire_run_lock(home, "task-001")
        held.adopt("run_going")

        with pytest.raises(RunLockTimeout) as caught:
            acquire_run_lock(home, "task-001", run_id="run_next", timeout=0.3)

        assert "run_going" in str(caught.value)
        held.release()

    def test_a_live_run_holds_it_even_when_the_recorded_pid_is_gone(self, home: Path) -> None:
        """The CLI case, and the reason the pid does not get a vote once a run is named.

        ``agentjobs dispatch run`` starts a session that deliberately outlives the shell
        that started it, so the pid in the lock is *expected* to be dead while the run
        goes on. Reclaiming on that would put a second agent on a task that already has
        one -- worse than the leak this fix is for.
        """
        seed_run(home, "task-001", run_id="run_session", mode="session", status="running")
        lock = acquire_run_lock(home, "task-001")
        lock.path.write_text("pid=999999999 run=run_session", encoding="ascii")

        with pytest.raises(RunLockTimeout):
            acquire_run_lock(home, "task-001", run_id="run_next", timeout=0.3)

    def test_a_lock_naming_no_run_is_reclaimed_only_when_its_process_is_gone(
        self, home: Path
    ) -> None:
        """The window between taking the lock and the run existing to be named."""
        path = locks_root(home)
        path.mkdir(parents=True, exist_ok=True)
        (path / "task-001.lock").write_text("pid=999999999 run=", encoding="ascii")

        taken = acquire_run_lock(home, "task-001", run_id="run_next", timeout=0.5)

        assert taken.run_id == "run_next"
        taken.release()

    def test_a_lock_naming_no_run_and_a_living_process_still_refuses(self, home: Path) -> None:
        held = acquire_run_lock(home, "task-001")  # this process, no run adopted

        with pytest.raises(RunLockTimeout):
            acquire_run_lock(home, "task-001", run_id="run_next", timeout=0.3)

        held.release()

    def test_a_lock_naming_a_run_nobody_has_a_record_of_falls_back_to_the_pid(
        self, home: Path
    ) -> None:
        """A run with no directory cannot be followed, concluded or cancelled by anything.

        Refusing on it forever would be the same permanent silent block, merely rarer.
        So the weaker evidence is consulted -- and it is still evidence: a living holder
        keeps the lock either way.
        """
        held = acquire_run_lock(home, "task-001")
        held.adopt("run_unknown")

        with pytest.raises(RunLockTimeout):
            acquire_run_lock(home, "task-001", run_id="run_next", timeout=0.3)

        held.path.write_text("pid=999999999 run=run_unknown", encoding="ascii")
        taken = acquire_run_lock(home, "task-001", run_id="run_next", timeout=0.5)
        taken.release()

    def test_the_refusal_never_tells_a_reader_to_delete_a_file(self, home: Path) -> None:
        """ac-5: this text reaches the browser, where that remedy does not exist.

        ``dispatch_task`` turns this exception into ``LiveRunExistsError`` and the task
        page renders the message verbatim. It used to end "delete the file to clear it",
        naming ``~/.agentjobs/runs/.locks/`` -- a directory nothing in the app mentions,
        holding a file that could not be deleted anyway.
        """
        seed_run(home, "task-001", run_id="run_going", status="running")
        held = acquire_run_lock(home, "task-001")
        held.adopt("run_going")

        with pytest.raises(RunLockTimeout) as caught:
            acquire_run_lock(home, "task-001", timeout=0.2)

        message = str(caught.value)
        assert "delete the file" not in message.lower()
        assert ".locks" not in message
        assert "run_going" in message
        assert "cancel" in message.lower()
        held.release()

    def test_a_stale_lock_times_out_rather_than_hanging(self, home: Path) -> None:
        """A hang tells you nothing. Unjudgeable is still refused, but never by blocking."""
        held = acquire_run_lock(home, "task-001", run_id="run_dead")

        started = time.monotonic()
        with pytest.raises(RunLockTimeout) as caught:
            acquire_run_lock(home, "task-001", timeout=0.3)
        elapsed = time.monotonic() - started

        assert elapsed < 5, "a stale lock must time out, not block"
        assert "run_dead" in str(caught.value)
        held.release()

    def test_releasing_twice_is_safe(self, home: Path) -> None:
        lock = acquire_run_lock(home, "task-001")
        lock.release()
        lock.release()

    def test_a_late_release_leaves_a_newer_runs_lock_alone(self, home: Path) -> None:
        """The window between a run's terminal write and its release.

        A second dispatch can legitimately reclaim the lock inside it. A blind unlink
        would then delete the *new* run's lock and let a third dispatch in beside it.
        """
        seed_run(home, "task-001", run_id="run_first", status="finished")
        first = acquire_run_lock(home, "task-001")
        first.adopt("run_first")
        second = acquire_run_lock(home, "task-001", run_id="run_second", timeout=0.5)

        first.release()

        assert second.path.exists()
        holder = read_lock_holder(second.path)
        assert holder is not None and holder.run_id == "run_second"

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


class TestStaleLockSweep:
    """The startup sweep, which is what makes a restart heal the leak it used to cause."""

    def test_it_releases_locks_whose_runs_have_ended(self, home: Path) -> None:
        seed_run(home, "task-001", run_id="run_over", status="finished")
        lock = acquire_run_lock(home, "task-001")
        lock.adopt("run_over")

        released = release_stale_locks(home)

        assert [(item.task_id, item.run_id) for item in released] == [("task-001", "run_over")]
        assert not lock.path.exists()

    def test_it_leaves_a_live_runs_lock_alone(self, home: Path) -> None:
        seed_run(home, "task-001", run_id="run_going", status="running")
        lock = acquire_run_lock(home, "task-001")
        lock.adopt("run_going")

        assert release_stale_locks(home) == []
        assert lock.path.exists()
        lock.release()

    def test_it_survives_a_home_that_has_never_dispatched(self, home: Path) -> None:
        assert release_stale_locks(home) == []

    def test_reconcile_concludes_the_run_before_judging_its_lock(
        self, home: Path, project: Project, manager: TaskManager, task
    ) -> None:
        """ac-3 and ac-4, as a unit: the order is what makes a restart clear the lock.

        A batch run orphaned by a restart still reads ``running`` on disk. Sweeping the
        locks first would find it live and leave every one of them behind -- the leak,
        one restart later. ``reconcile`` marks it ``interrupted`` and *then* judges.
        """
        record = seed_run(home, task.id, run_id="run_orphan", mode="batch", status="running")
        dispatch_entry(manager, task.id, "run_orphan")
        lock = acquire_run_lock(home, task.id)
        lock.adopt("run_orphan")

        ledger = DispatchLedger(home, registry=ProjectRegistry(home=home))
        details = [result.detail for result in ledger.reconcile()]

        assert not lock.path.exists(), "a restart must not strand the lock"
        assert any("released the run lock" in detail for detail in details)
        assert read_run(record.path).outcome == "interrupted"

        # And the point of all of it: the task can be dispatched again.
        again = acquire_run_lock(home, task.id, run_id="run_next", timeout=0.5)
        again.release()


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
        runner_module._kill_tree = lambda pid: killed.append(pid)
        try:
            ledger.cancel("run_test0001")
        finally:
            runner_module._kill_tree = original

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

        def observe(record):
            seen.append(sentinel_path(home).exists())
            return original(record)

        ledger._stop = observe  # type: ignore[method-assign]
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
    def test_a_finished_session_is_removed(
        self, home: Path, closed_task, fake_session_cli: Path
    ) -> None:
        seed_run(home, closed_task.id, mode="session", session_id="s1", status="finished")

        results = ledger_with(home, fake_session_cli).reap_finished()

        assert [r.stopped for r in results] == [True]
        assert read_run(home / "runs" / "run_test0001").path.exists()

    def test_a_refused_reap_says_so_and_is_never_forced(
        self, home: Path, closed_task, fake_session_cli: Path
    ) -> None:
        """A refusal is surfaced verbatim rather than retried with force.

        Since task-186 a dispatched session owns no worktree, so this refusal is no
        longer the routine "a run produced work nobody has looked at" signal it was
        written for -- a session AgentJobs did not start can still own one, and a
        refusal can also be a transient file handle. Either way the answer is the same
        and is the point of the test: report it, do not pass ``-f``.
        """
        (fake_session_cli.parent / "dirty").write_text("", encoding="utf-8")
        seed_run(home, closed_task.id, mode="session", session_id="s1", status="finished")

        results = ledger_with(home, fake_session_cli).reap_finished()

        assert not results[0].stopped
        assert "uncommitted" in results[0].detail
        meta = yaml.safe_load((home / "runs" / "run_test0001" / "meta.yaml").read_text())
        assert "uncommitted" in meta["reap_blocked"]

    def test_a_reaped_session_is_not_reaped_again(
        self, home: Path, closed_task, fake_session_cli: Path
    ) -> None:
        seed_run(home, closed_task.id, mode="session", session_id="s1", status="finished")
        ledger = ledger_with(home, fake_session_cli)

        ledger.reap_finished()
        second = ledger.reap_finished()

        assert second == []

    def test_reaping_issues_exactly_one_session_removal_and_nothing_else(
        self, home: Path, closed_task, fake_session_cli: Path
    ) -> None:
        """task-186's coherence check: reap narrowed, it did not become a no-op.

        What it does now is remove the finished session's row, freeing the pid that row
        holds. What it must *not* have grown is a second step going after directories --
        the worktree a dispatched agent makes for itself is outside AgentJobs' knowledge
        and is the agent's to remove. So this asserts the exact call issued, which is
        the only way to tell "narrowed on purpose" from "quietly does nothing".
        """
        seed_run(home, closed_task.id, mode="session", session_id="s1", status="finished")

        results = ledger_with(home, fake_session_cli).reap_finished()

        assert [r.stopped for r in results] == [True]
        calls = json.loads((fake_session_cli.parent / "calls.json").read_text())
        assert calls == [["rm", "s1"]]

    def test_batch_runs_are_never_reaped(
        self, home: Path, closed_task, fake_session_cli: Path
    ) -> None:
        """There is no session to remove; the process is already gone."""
        seed_run(home, closed_task.id, mode="batch", status="finished")

        assert ledger_with(home, fake_session_cli).reap_finished() == []

    def test_a_live_session_is_never_reaped(
        self, home: Path, closed_task, fake_session_cli: Path
    ) -> None:
        seed_run(home, closed_task.id, mode="session", session_id="s1", status="running")

        assert ledger_with(home, fake_session_cli).reap_finished() == []


class TestReapKeepsWhatCanStillBeWoken:
    """task-234. Reaping calls ``claude rm``, which deletes the conversation -- and the
    conversation is what the next dispatch of an open task resumes instead of booting a
    cold agent. So the sweep now asks "can anything still want this session back".
    """

    def test_an_open_tasks_newest_session_is_kept(
        self, home: Path, task, fake_session_cli: Path
    ) -> None:
        seed_run(home, task.id, mode="session", session_id="s1", status="finished")

        assert ledger_with(home, fake_session_cli).reap_finished() == []
        # No calls file at all: the session manager was never invoked, which is a
        # stronger statement than "it was invoked and removed nothing".
        assert not (fake_session_cli.parent / "calls.json").exists()

    def test_an_open_tasks_older_sessions_are_still_reaped(
        self, home: Path, task, fake_session_cli: Path
    ) -> None:
        """Only one is wakeable, so only one is kept. The rest are litter.

        Without this the pile grows without bound for any long-lived task: a session per
        run, forever, none of which anything will ever resume.
        """
        seed_run(
            home,
            task.id,
            run_id="run_old",
            mode="session",
            session_id="old",
            status="finished",
            started_at="2026-08-18T08:00:00+00:00",
        )
        seed_run(
            home,
            task.id,
            run_id="run_new",
            mode="session",
            session_id="new",
            status="finished",
            started_at="2026-08-19T08:00:00+00:00",
        )

        results = ledger_with(home, fake_session_cli).reap_finished()

        assert [r.run_id for r in results] == ["run_old"]
        assert json.loads((fake_session_cli.parent / "calls.json").read_text()) == [["rm", "old"]]

    def test_closing_the_task_releases_the_kept_session(
        self, home: Path, manager: TaskManager, task, fake_session_cli: Path
    ) -> None:
        """The collection point. A kept session is deferred, not exempt."""
        seed_run(home, task.id, mode="session", session_id="s1", status="finished")
        ledger = ledger_with(home, fake_session_cli)
        assert ledger.reap_finished() == []

        manager.close_task(task.id, actor="claude", outcome=Outcome.COMPLETED)

        assert [r.stopped for r in ledger.reap_finished()] == [True]

    def test_a_run_whose_task_cannot_be_resolved_is_reaped(
        self, home: Path, fake_session_cli: Path
    ) -> None:
        """Keeping every unattributable session would be hoarding, not caution."""
        seed_run(home, "task-does-not-exist", mode="session", session_id="s1", status="finished")

        assert [r.stopped for r in ledger_with(home, fake_session_cli).reap_finished()] == [True]


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


# ----- reaping at startup -----------------------------------------------------


class TestStartupReaping:
    """A worktree belonging to a finished run is litter, and startup is where it goes.

    Nothing in AgentJobs schedules background work, so reaping happens at server
    startup and on demand. These pin that the startup path actually calls it, that a
    refusal is reported rather than swallowed, and that neither can take the server down.
    """

    def test_a_finished_session_worktree_is_removed_at_startup(
        self, home: Path, closed_task, fake_session_cli: Path, capsys
    ) -> None:
        from agentjobs.api.main import _reap_finished_sessions

        seed_run(home, closed_task.id, mode="session", session_id="s1", status="finished")

        _reap_finished_sessions(ledger_with(home, fake_session_cli))

        meta = yaml.safe_load((home / "runs" / "run_test0001" / "meta.yaml").read_text())
        assert meta["reaped"] is True
        assert "reaped run_test0001" in capsys.readouterr().out

    def test_a_worktree_holding_uncommitted_work_is_kept_and_said_so(
        self, home: Path, closed_task, fake_session_cli: Path, capsys
    ) -> None:
        """The refusal is the signal: that run produced work nobody has looked at."""
        from agentjobs.api.main import _reap_finished_sessions

        (fake_session_cli.parent / "dirty").write_text("", encoding="utf-8")
        seed_run(home, closed_task.id, mode="session", session_id="s1", status="finished")

        _reap_finished_sessions(ledger_with(home, fake_session_cli))

        out = capsys.readouterr().out
        assert "kept run_test0001" in out
        assert "uncommitted" in out

    def test_reconciliation_reaps_as_well_as_reconciles(
        self, home: Path, closed_task, monkeypatch, capsys
    ) -> None:
        """Wiring, asserted directly: deleting the call is otherwise invisible."""
        from agentjobs.api import main as api_main

        monkeypatch.setattr(api_main, "default_home", lambda: home)
        called: List[object] = []
        monkeypatch.setattr(
            api_main, "_reap_finished_sessions", lambda ledger: called.append(ledger)
        )
        seed_run(home, closed_task.id, mode="batch", status="finished")

        api_main._reconcile_dispatch_runs()

        assert len(called) == 1

    def test_a_session_manager_that_cannot_be_run_does_not_take_the_server_down(
        self, home: Path, closed_task, capsys
    ) -> None:
        from agentjobs.api.main import _reap_finished_sessions

        seed_run(home, closed_task.id, mode="session", session_id="s1", status="finished")
        ledger = DispatchLedger(
            home,
            registry=ProjectRegistry(home=home),
            session_command=["definitely-not-a-real-binary"],
        )

        _reap_finished_sessions(ledger)

        # Reported as kept, not raised: a server that refuses to start because it could
        # not tidy up is worse than one that starts with the tidying undone.
        assert "kept run_test0001" in capsys.readouterr().out
