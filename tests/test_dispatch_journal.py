"""Dispatch through the execution journal: the task-264 races, end to end.

Each class is one acceptance criterion and drives the real dispatch code -- runner,
ledger, poller, guards -- against a real task store and a real journal. The races are
forced deterministically: the losing side is held at the exact point the old code raced
at, the winning side runs to completion, and then the loser is released.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional, TypeVar

import pytest
import yaml

from agentjobs.dispatch import journal
from agentjobs.dispatch.config import assert_dispatch_permitted
from agentjobs.dispatch.interactive import settle_for_task
from agentjobs.dispatch.ledger import (
    DispatchLedger,
    acquire_run_lock,
    legacy_run_lock_path,
    lock_name,
    read_run,
    run_lock_path,
    stale_lock_reason,
    read_lock_holder,
    write_status,
)
from agentjobs.dispatch.poller import poll_live_sessions
from agentjobs.dispatch.runner import DispatchRunner, RunDirectory, new_run_id
from agentjobs.dispatch.wake import find_wake_target
from agentjobs.execution.errors import OwnerModeConflict, OwnershipConflict
from agentjobs.execution.coordinator import import_source_events
from agentjobs.execution.store import OWNER_DURABLE
from agentjobs.manager import DuplicateDispatchResultError, TaskManager
from agentjobs.models_v2 import (
    Ball,
    BallReason,
    DispatchOutcome,
    Lifecycle,
    LogEntry,
    LogEntryType,
)
from agentjobs.projects import ProjectRegistry
from support import task_store

from test_dispatch_poller import _dispatch_yaml, _dispatched_task, _run_meta, _set_ledger
from test_dispatch_runner import FAKE_CLI, write_script

_T = TypeVar("_T")


def must(value: Optional[_T]) -> _T:
    """The value, asserted present -- a lookup the test has just made true."""
    assert value is not None
    return value


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The poller tests' machine: a registered project, a dispatch home, a fake session CLI."""
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
    monkeypatch.setenv("AGENTJOBS_HOME", str(home))
    ProjectRegistry(home=home).add(root, project_id="sandbox")
    manager = TaskManager(task_store(root / "tasks", project_id="sandbox"))
    return home, root, manager, fake_cli


def results_for(manager: TaskManager, task_id: str, run_id: str) -> List[LogEntry]:
    task = must(manager.get_task(task_id))
    return [
        entry
        for entry in task.log
        if entry.type is LogEntryType.DISPATCH_RESULT and entry.data.get("run_id") == run_id
    ]


def start_admitted_session(sandbox) -> tuple[str, str]:
    """A session started the way ``dispatch_task`` starts one: admitted, then launched."""
    home, root, manager, _ = sandbox
    task_id = _dispatched_task(manager)
    run_id = new_run_id()
    journal.journal(home).admit(
        project_id="sandbox",
        task_id=task_id,
        run_id=run_id,
        capacity=3,
        envelope={"runner": "fake"},
        workflow_version=1,
    )
    runner = DispatchRunner(
        manager=manager,
        resolution=assert_dispatch_permitted("sandbox", home),
        project_root=root,
        home=home,
    )
    runner.start(must(manager.get_task(task_id)), actor="Jeff Posey", caused_by=1, run_id=run_id)
    return run_id, task_id


def reached_or_finished(reached: threading.Event, worker: threading.Thread) -> bool:
    """Wait until ``reached`` is set or ``worker`` has ended, and say which.

    No budget, deliberately (task-522, flake register entry 8). ``reached.wait(10)`` read
    a loaded machine as a poller that never arrived: the gate log shows the same poller
    arriving after the ten seconds and then waiting on a release the failed test never
    sent. "Never" is only observable as the thread ending without arriving, so that is
    the question asked. The wait is bounded by the code under test, not by the test:
    every subprocess the poll runs carries the runner's own 60-second timeout.
    """
    while not reached.wait(0.05):
        if not worker.is_alive():
            return reached.is_set()
    return True


# ----- ac-1: a cancel landing mid-poll -------------------------------------------------


class TestACancelLandingMidPoll:
    """task-107's two endings for one run, reproduced and refused.

    The poller is released only after the cancellation has concluded, which is the
    interleaving that wrote ``cancelled`` and then ``interrupted`` eleven seconds apart.
    """

    def _hold_the_poller_at_its_conclusion(self, monkeypatch):
        reached = threading.Event()
        release = threading.Event()
        original = journal.claim_conclusion

        def held(home, record, outcome, **kwargs):
            if kwargs.get("concluded_by") == "poller":
                reached.set()
                # Unbounded: the test releases this in a `finally`, whatever happens.
                release.wait()
            return original(home, record, outcome, **kwargs)

        monkeypatch.setattr(journal, "claim_conclusion", held)
        return reached, release

    def test_the_cancel_wins_and_the_poll_writes_nothing(self, sandbox, monkeypatch) -> None:
        home, _, manager, fake_cli = sandbox
        run_id, task_id = start_admitted_session(sandbox)
        # The session finished its turn and handed off, so the poll will try to conclude it.
        manager.handoff(
            task_id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Look.",
        )
        _set_ledger(fake_cli, [{"id": "b55b35ad", "status": "idle", "state": "done"}])
        reached, release = self._hold_the_poller_at_its_conclusion(monkeypatch)
        # Counted at the manager, not read off the log: the manager's own refusal of a
        # second ending would hide a broken compare-and-set from a log-only assertion.
        attempts_to_write: List[str] = []
        original_record = TaskManager.record_dispatch_result

        def counting(self, task, **kwargs):
            attempts_to_write.append(kwargs["outcome"].value)
            return original_record(self, task, **kwargs)

        monkeypatch.setattr(TaskManager, "record_dispatch_result", counting)

        poller = threading.Thread(target=poll_live_sessions, args=(home,))
        poller.start()
        try:
            assert reached_or_finished(reached, poller), "the poller never reached its conclusion"

            ledger = DispatchLedger(
                home,
                session_command=[sys.executable, str(fake_cli)],
                managers={"sandbox": manager},
            )
            cancelled = ledger.cancel(
                run_id, actor="dispatcher", source="test", requester="Jeff Posey"
            )
            assert cancelled.stopped
        finally:
            release.set()
            poller.join()

        (only,) = results_for(manager, task_id, run_id)
        assert only.data["outcome"] == "cancelled"
        assert attempts_to_write == ["cancelled"], "the poll lost the set and wrote nothing"
        meta = _run_meta(home, run_id)
        assert meta["outcome"] == "cancelled" and meta["status"] == "cancelled"
        attempt = must(journal.journal(home).attempt(run_id))
        assert attempt.outcome == "cancelled"
        request = must(attempt.cancel)
        assert request["requester"] == "Jeff Posey"
        assert request["source"] == "test"

    def test_a_poll_that_sees_the_requested_cancel_defers_to_it(self, sandbox) -> None:
        """The guard ``_finish_batch`` always had, now on the session path too."""
        home, root, manager, fake_cli = sandbox
        run_id, task_id = start_admitted_session(sandbox)
        record = read_run(home / "runs" / run_id)
        journal.request_cancel(home, record, requester="Jeff Posey", source="gui")
        manager.handoff(
            task_id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Look.",
        )
        _set_ledger(fake_cli, [{"id": "b55b35ad", "status": "idle", "state": "done"}])

        poll_live_sessions(home)

        assert results_for(manager, task_id, run_id) == [], "the cancellation owns the ending"
        assert must(journal.journal(home).attempt(run_id)).is_live

    def test_the_poll_wins_and_the_cancel_finds_it_already_over(self, sandbox) -> None:
        home, _, manager, fake_cli = sandbox
        run_id, task_id = start_admitted_session(sandbox)
        manager.handoff(
            task_id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Look.",
        )
        _set_ledger(fake_cli, [{"id": "b55b35ad", "status": "idle", "state": "done"}])
        poll_live_sessions(home)

        ledger = DispatchLedger(
            home, session_command=[sys.executable, str(fake_cli)], managers={"sandbox": manager}
        )
        late = ledger.cancel(run_id)
        assert not late.stopped and "already" in late.detail
        (only,) = results_for(manager, task_id, run_id)
        assert only.data["outcome"] == "completed"
        assert _run_meta(home, run_id)["outcome"] == "completed"

    def test_a_late_non_terminal_meta_write_cannot_revive_a_concluded_run(self, sandbox) -> None:
        """The lost update: a poll tick's ``status: stalled`` from a stale read."""
        home, _, _, _ = sandbox
        run_id, _ = start_admitted_session(sandbox)
        record = read_run(home / "runs" / run_id)
        write_status(record, status="cancelled", outcome="cancelled")
        RunDirectory(home / "runs" / run_id).update_meta(status="stalled", stalled_at="now")
        meta = _run_meta(home, run_id)
        assert meta["status"] == "cancelled" and meta["outcome"] == "cancelled"
        assert meta["stalled_at"] == "now", "facts may still be added"

    def test_the_manager_refuses_a_second_ending_for_one_run(self, sandbox) -> None:
        _, _, manager, _ = sandbox
        task_id = _dispatched_task(manager)
        manager.record_dispatch_result(
            task_id, actor="dispatcher", run_id="run_x", outcome=DispatchOutcome.CANCELLED
        )
        with pytest.raises(DuplicateDispatchResultError):
            manager.record_dispatch_result(
                task_id, actor="dispatcher", run_id="run_x", outcome=DispatchOutcome.INTERRUPTED
            )

    def test_a_swept_run_says_how_it_ended(self, sandbox) -> None:
        """P4: ``interrupted`` is not ``cancelled``."""
        home, _, manager, fake_cli = sandbox
        run_id, task_id = start_admitted_session(sandbox)
        _set_ledger(fake_cli, [])  # the session manager no longer knows it
        ledger = DispatchLedger(
            home, session_command=[sys.executable, str(fake_cli)], managers={"sandbox": manager}
        )
        ledger.reconcile()
        meta = _run_meta(home, run_id)
        assert meta["outcome"] == "interrupted"
        assert meta["status"] == "failed"
        (only,) = results_for(manager, task_id, run_id)
        assert only.data["outcome"] == "interrupted"


# ----- task-370: a Stop recorded after the guard read, before the set --------------------


def _hold_conclusions(monkeypatch) -> Dict[str, tuple[threading.Event, threading.Event]]:
    """Park every ``claim_conclusion`` at its call, keyed by who is concluding.

    ``ledger`` covers every ``ledger: <actor>`` concluder. Each entry is (reached,
    release); a caller not in the table passes straight through.
    """
    gates = {
        name: (threading.Event(), threading.Event())
        for name in ("poller", "batch supervisor", "ledger")
    }
    original = journal.claim_conclusion

    def held(home, record, outcome, **kwargs):
        by = str(kwargs.get("concluded_by") or "")
        key = "ledger" if by.startswith("ledger") else by
        if key in gates:
            reached, release = gates[key]
            reached.set()
            assert release.wait(20), f"the test never released {key}"
        return original(home, record, outcome, **kwargs)

    monkeypatch.setattr(journal, "claim_conclusion", held)
    return gates


class TestAStopRecordedBetweenTheGuardAndTheSet:
    """task-370: the concluder that read "no Stop" and then reached the set first.

    ``_finish_batch`` and ``_finish_session`` each ask the journal whether a Stop is on
    record and *then*, in a separate transaction, claim the conclusion. A Stop recorded
    between the two was invisible to the guard, and the compare-and-set did not look at
    it either -- so the supervisor won with ``failed`` (or the poll with ``completed``),
    the cancellation lost, and the person who pressed Cancel was told ``stopped: true``
    beside an outcome that said otherwise. That is the gate's
    ``assert 'failed' == 'cancelled'``, reproduced here on purpose.

    Both orders of the final writes are now driven: the ledger-first order is
    ``TestACancelLandingMidPoll``; this class is the concluder-first order.
    """

    def test_a_batch_run_that_exits_as_it_is_cancelled_still_says_cancelled(
        self, sandbox, monkeypatch, tmp_path: Path
    ) -> None:
        from test_dispatch_runner import make_resolution

        home, root, manager, fake_cli = sandbox
        task_id = _dispatched_task(manager)
        run_id = new_run_id()
        journal.journal(home).admit(
            project_id="sandbox", task_id=task_id, run_id=run_id, capacity=3
        )
        gates = _hold_conclusions(monkeypatch)
        # Exits on its own, non-zero: the supervisor has a real `failed` to write, and the
        # pid the ledger goes on to kill is already gone -- the busy-machine case.
        script = write_script(tmp_path / "exits.py", "import sys\nsys.exit(3)\n")
        runner = DispatchRunner(
            manager=manager,
            resolution=make_resolution([sys.executable, str(script), "{prompt}"]),
            project_root=root,
            home=home,
        )
        handle = runner.start(
            must(manager.get_task(task_id)), actor="Jeff Posey", caused_by=1, run_id=run_id
        )
        supervisor_reached, supervisor_release = gates["batch supervisor"]
        assert supervisor_reached.wait(20), "the supervisor never reached its conclusion"

        ledger = DispatchLedger(
            home, session_command=[sys.executable, str(fake_cli)], managers={"sandbox": manager}
        )
        outcome: Dict[str, object] = {}
        cancelling = threading.Thread(
            target=lambda: outcome.update(result=ledger.cancel(run_id, requester="Jeff Posey"))
        )
        cancelling.start()
        ledger_reached, ledger_release = gates["ledger"]
        assert ledger_reached.wait(20), "the cancellation never reached its conclusion"

        # The supervisor read "no Stop" before the Stop existed, and reaches the set first.
        supervisor_release.set()
        assert handle.supervisor is not None
        handle.supervisor.join(20)
        assert not handle.supervisor.is_alive()
        ledger_release.set()
        cancelling.join(20)
        assert not cancelling.is_alive()

        assert outcome["result"].stopped  # type: ignore[attr-defined]
        (only,) = results_for(manager, task_id, run_id)
        assert only.data["outcome"] == "cancelled"
        assert must(journal.journal(home).attempt(run_id)).outcome == "cancelled"
        assert _run_meta(home, run_id)["outcome"] == "cancelled"

    def test_a_poll_that_reaches_the_set_after_a_stop_defers_to_it(
        self, sandbox, monkeypatch
    ) -> None:
        home, _, manager, fake_cli = sandbox
        run_id, task_id = start_admitted_session(sandbox)
        manager.handoff(
            task_id,
            actor="claude",
            ball=Ball.HUMAN,
            ball_reason=BallReason.REVIEW,
            ball_prompt="Look.",
        )
        _set_ledger(fake_cli, [{"id": "b55b35ad", "status": "idle", "state": "done"}])
        gates = _hold_conclusions(monkeypatch)

        poller = threading.Thread(target=poll_live_sessions, args=(home,))
        poller.start()
        poll_reached, poll_release = gates["poller"]
        assert poll_reached.wait(20), "the poller never reached its conclusion"

        ledger = DispatchLedger(
            home, session_command=[sys.executable, str(fake_cli)], managers={"sandbox": manager}
        )
        outcome: Dict[str, object] = {}
        cancelling = threading.Thread(
            target=lambda: outcome.update(result=ledger.cancel(run_id, requester="Jeff Posey"))
        )
        cancelling.start()
        ledger_reached, ledger_release = gates["ledger"]
        assert ledger_reached.wait(20), "the cancellation never reached its conclusion"

        poll_release.set()
        poller.join(20)
        assert not poller.is_alive()
        ledger_release.set()
        cancelling.join(20)
        assert not cancelling.is_alive()

        assert outcome["result"].stopped  # type: ignore[attr-defined]
        (only,) = results_for(manager, task_id, run_id)
        assert only.data["outcome"] == "cancelled"
        assert _run_meta(home, run_id)["outcome"] == "cancelled"

    def test_a_conclusion_that_does_not_defer_is_not_refused(self, sandbox) -> None:
        """The deferral is opt-in: a sweep concluding a Stop nobody finished still ends it."""
        home, _, _, _ = sandbox
        run_id, _ = start_admitted_session(sandbox)
        record = read_run(home / "runs" / run_id)
        journal.request_cancel(home, record, requester="Jeff Posey", source="gui")

        deferred = journal.claim_conclusion(
            home,
            record,
            DispatchOutcome.FAILED,
            concluded_by="batch supervisor",
            defer_to_cancel=True,
        )
        assert not deferred.won
        swept = journal.claim_conclusion(
            home, record, DispatchOutcome.INTERRUPTED, concluded_by="ledger: sweep"
        )
        assert swept.won


# ----- auditor 12: a worker's own meta is not authority --------------------------------


class TestAWorkerCannotDeclareItselfOver:
    def test_a_status_written_into_its_own_meta_frees_neither_lock_nor_task(self, sandbox) -> None:
        home, _, manager, _ = sandbox
        run_id, task_id = start_admitted_session(sandbox)
        lock = acquire_run_lock(home, task_id, project_id="sandbox", run_id=run_id, timeout=1)
        directory = RunDirectory(home / "runs" / run_id)
        forged = directory.read_meta()
        forged.update(status="finished", outcome="completed")
        directory.write_meta(forged)  # what an agent in the run directory can do

        holder = read_lock_holder(lock.path)
        assert stale_lock_reason(home, must(holder)) is None, "the lock is still held"
        released = journal.release_ended(home, lambda project: manager)
        assert released == []
        with pytest.raises(OwnershipConflict):
            journal.journal(home).admit(
                project_id="sandbox", task_id=task_id, run_id="run_second", capacity=3
            )
        lock.release()


# ----- ac-3 / durable-2: two projects, one task id -------------------------------------


@pytest.fixture
def two_projects(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AGENTJOBS_HOME", str(home))
    managers: Dict[str, TaskManager] = {}
    for project_id in ("alpha", "beta"):
        root = tmp_path / project_id
        (root / ".agentjobs").mkdir(parents=True)
        (root / "tasks").mkdir()
        (root / ".agentjobs" / "config.yaml").write_text(
            yaml.safe_dump({"project_name": project_id, "tasks_directory": "tasks"}),
            encoding="utf-8",
        )
        ProjectRegistry(home=home).add(root, project_id=project_id)
        manager = TaskManager(task_store(root / "tasks", project_id=project_id))
        created = manager.create_task(
            title="Same id",
            category="general",
            summary="s",
            description="d",
            lifecycle=Lifecycle.READY,
        )
        assert created.id == "task-001"
        managers[project_id] = manager
    return home, managers


def seed_session_run(home: Path, run_id: str, project_id: str, session: str, *, live: bool) -> None:
    RunDirectory.create(
        home,
        run_id,
        {
            "run_id": run_id,
            "task_id": "task-001",
            "project_id": project_id,
            "mode": "session",
            "status": "running" if live else "finished",
            "session_id": session,
            "started_at": "2026-09-13T08:00:00+00:00",
        },
    )


class TestTwoProjectsWithOneTaskId:
    def test_their_run_locks_are_different_files_and_never_contend(self, two_projects) -> None:
        home, _ = two_projects
        alpha = acquire_run_lock(home, "task-001", project_id="alpha", timeout=0.2)
        beta = acquire_run_lock(home, "task-001", project_id="beta", timeout=0.2)
        assert alpha.path != beta.path
        assert alpha.path.name == f"{lock_name('alpha', 'task-001')}.lock"
        alpha.release()
        beta.release()

    def test_a_pre_upgrade_lock_binds_its_own_project_only(self, two_projects) -> None:
        home, _ = two_projects
        seed_session_run(home, "run_old", "alpha", "s-old", live=True)
        legacy = legacy_run_lock_path(home, "task-001")
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text("pid=1 run=run_old kind=dispatch", encoding="ascii")

        with pytest.raises(Exception, match="run_old"):
            acquire_run_lock(home, "task-001", project_id="alpha", timeout=0.2)
        beta = acquire_run_lock(home, "task-001", project_id="beta", timeout=0.2)
        beta.release()
        assert not run_lock_path(home, "task-001", project_id="alpha").exists()

    def test_ownership_admission_and_live_scans_are_per_project(self, two_projects) -> None:
        home, managers = two_projects
        seed_session_run(home, "run_alpha", "alpha", "s-a", live=True)
        view = journal.legacy_view(home, project_id="beta", task_id="task-001")
        assert view.owners == (), "alpha's live run is not beta's owner"
        assert journal.legacy_view(home, project_id="alpha", task_id="task-001").owners == (
            "run_alpha",
        )
        attempt = journal.admit_dispatch(
            home,
            project_id="beta",
            task_id="task-001",
            run_id="run_beta",
            capacity=3,
            hourly_limit=None,
            envelope={},
            mode="session",
            resolve_manager=managers.get,
        )
        assert attempt.project_id == "beta"
        with pytest.raises(OwnershipConflict):
            journal.admit_dispatch(
                home,
                project_id="alpha",
                task_id="task-001",
                run_id="run_alpha_2",
                capacity=3,
                hourly_limit=None,
                envelope={},
                mode="session",
                resolve_manager=managers.get,
            )

    def test_a_wake_resumes_its_own_projects_conversation(self, two_projects) -> None:
        home, _ = two_projects
        seed_session_run(home, "run_alpha", "alpha", "aaaa1111", live=False)
        seed_session_run(home, "run_beta", "beta", "bbbb2222", live=False)
        rows = [
            {"id": "aaaa1111", "sessionId": "aaaa1111-uuid"},
            {"id": "bbbb2222", "sessionId": "bbbb2222-uuid"},
        ]
        for project_id, uuid in (("alpha", "aaaa1111-uuid"), ("beta", "bbbb2222-uuid")):
            target = find_wake_target(home, "task-001", project_id=project_id, rows=rows)
            assert target is not None and target.session_uuid == uuid
        assert find_wake_target(home, "task-001", project_id="gamma", rows=rows) is None

    def test_the_reaper_keeps_each_projects_newest_conversation(self, two_projects) -> None:
        home, managers = two_projects
        seed_session_run(home, "run_alpha", "alpha", "aaaa1111", live=False)
        seed_session_run(home, "run_beta", "beta", "bbbb2222", live=False)
        ledger = DispatchLedger(home, managers=managers)
        assert ledger._wakeable_run_ids() == {"run_alpha", "run_beta"}
        managers["beta"].claim_task("task-001", agent="claude")
        managers["beta"].close_task("task-001", actor="claude", outcome="completed", body="done")
        assert ledger._wakeable_run_ids() == {"run_alpha"}

    def test_settling_one_projects_task_leaves_the_other_projects_session(
        self, two_projects
    ) -> None:
        home, managers = two_projects
        for project_id in ("alpha", "beta"):
            RunDirectory.create(
                home,
                f"run_{project_id}",
                {
                    "run_id": f"run_{project_id}",
                    "task_id": "task-001",
                    "project_id": project_id,
                    "mode": "interactive",
                    "status": "running",
                    "session_id": f"s-{project_id}",
                    "started_at": "2026-09-13T08:00:00+00:00",
                },
            )
        settled = settle_for_task(home, None, "task-001", project_id="alpha")
        assert [result.run_id for result in settled] == ["run_alpha"]
        assert read_run(home / "runs" / "run_beta").is_live


# ----- durable-3: crash windows between the task store and the journal ------------------


class TestCrashWindowsAgainstTheTaskStore:
    def test_a_handoff_committed_before_the_import_crashed_is_still_imported_once(
        self, two_projects
    ) -> None:
        home, managers = two_projects
        manager = managers["alpha"]
        manager.claim_task("task-001", agent="claude")
        manager.handoff(
            "task-001",
            actor="Jeff Posey",
            ball=Ball.AGENT,
            ball_reason=BallReason.REVISE,
            ball_prompt="Again.",
        )
        store = journal.journal(home)

        def die(label: str) -> None:
            if label == "import":
                raise RuntimeError("host died after the task store committed")

        store.before_commit = die
        with pytest.raises(RuntimeError):
            import_source_events(store, "alpha", journal.task_feed(manager))
        store.before_commit = None
        assert store.inbox() == []

        import_source_events(store, "alpha", journal.task_feed(manager))
        import_source_events(store, "alpha", journal.task_feed(manager))
        handoffs = [item for item in store.inbox() if item.kind == "handoff"]
        assert len(handoffs) == 1 and handoffs[0].status == "pending"
        entry_ids = [item.payload["entry_id"] for item in store.inbox()]
        assert entry_ids == sorted(set(entry_ids)), "every entry once, in feed order"

    def test_a_result_written_before_the_acknowledgement_crashed_is_not_written_twice(
        self, two_projects
    ) -> None:
        home, managers = two_projects
        manager = managers["alpha"]
        manager.claim_task("task-001", agent="claude")
        run = home / "runs" / "run_a"
        RunDirectory.create(
            home,
            "run_a",
            {
                "run_id": "run_a",
                "task_id": "task-001",
                "project_id": "alpha",
                "mode": "batch",
                "status": "running",
            },
        )
        store = journal.journal(home)
        store.admit(project_id="alpha", task_id="task-001", run_id="run_a", capacity=3)
        record = read_run(run)
        projection = journal.result_projection(
            record,
            DispatchOutcome.FAILED,
            actor="dispatcher",
            re=None,
            duration_seconds=1.0,
            body="x",
        )
        assert journal.claim_conclusion(
            home, record, DispatchOutcome.FAILED, concluded_by="t", projection=projection
        ).won

        def die(label: str) -> None:
            if label == "acknowledge":
                raise RuntimeError("host died after the task write, before the ack")

        store.before_commit = die
        with pytest.raises(RuntimeError):
            journal.deliver_projection(home, manager, projection)
        store.before_commit = None
        assert len(results_for(manager, "task-001", "run_a")) == 1
        assert [item.operation_id for item in store.pending_outbox()] == [projection.operation_id]

        delivered = journal.flush_owed_results(home, managers.get)
        assert delivered == [projection.operation_id]
        assert len(results_for(manager, "task-001", "run_a")) == 1, "replayed, not duplicated"
        assert store.pending_outbox() == []


# ----- durable-5: legacy migration ------------------------------------------------------


class TestLegacyMigration:
    def test_repeatable_with_provenance_unknowns_and_no_second_controller(
        self, two_projects
    ) -> None:
        home, _ = two_projects
        RunDirectory.create(
            home,
            "run_done",
            {
                "run_id": "run_done",
                "task_id": "task-001",
                "project_id": "alpha",
                "mode": "batch",
                "posture": "auto",
                "status": "finished",
                "outcome": "completed",
                "started_at": "2026-09-01T08:00:00+00:00",
            },
        )
        seed_session_run(home, "run_live", "beta", "bbbb2222", live=True)

        preview = journal.migrate_legacy_runs(home, dry_run=True)
        assert {r.disposition for r in preview} == {"would_import"}
        assert journal.journal(home).executions() == []

        first = {r.run_id: r for r in journal.migrate_legacy_runs(home)}
        second = {r.run_id: r for r in journal.migrate_legacy_runs(home)}
        assert {r.disposition for r in first.values()} == {"imported"}
        assert {r.disposition for r in second.values()} == {"already"}
        assert "runner" in first["run_done"].unknown_fields
        assert "posture" not in first["run_done"].unknown_fields

        store = journal.journal(home)
        executions = {e.task_id + "@" + e.project_id: e for e in store.executions()}
        done = executions["task-001@alpha"]
        assert done.provenance == "legacy_import" and done.terminal
        assert done.envelope == {"legacy_meta": {"mode": "batch", "posture": "auto"}}
        live = executions["task-001@beta"]
        assert live.owner_mode == "legacy" and not live.terminal
        with pytest.raises(OwnerModeConflict):
            store.claim_controller(live.execution_id, owner_mode=OWNER_DURABLE)
        assert must(store.attempt("run_live")).is_live


# ----- the shadow controller ----------------------------------------------------------


class TestTheShadowTick:
    def test_it_imports_signals_for_open_executions_and_performs_nothing(self, sandbox) -> None:
        home, _, manager, _ = sandbox
        old_task = _dispatched_task(manager)  # history from before any execution
        run_id, task_id = start_admitted_session(sandbox)
        manager.handoff(
            task_id,
            actor="Jeff Posey",
            ball=Ball.AGENT,
            ball_reason=BallReason.REVISE,
            ball_prompt="Once more.",
        )

        report = journal.shadow_tick(home, lambda project: manager)

        assert report.errors == ()
        store = journal.journal(home)
        pending = store.inbox(status="pending")
        assert task_id in {item.task_id for item in pending}
        execution = must(store.open_execution("sandbox", task_id))
        kinds = [event.kind for event in store.events(execution.execution_id)]
        assert "signal" in kinds and "launched" in kinds
        assert all(activity.shadow for activity in store.activities())
        assert must(store.attempt(run_id)).is_live, "a shadow pass concludes nothing"
        again = journal.shadow_tick(home, lambda project: manager)
        assert again.imported == 0 and again.advanced == ()
        assert old_task  # created before, and not a reason to fail the import

    def test_history_older_than_every_open_execution_is_passed_over(self, sandbox) -> None:
        from datetime import datetime, timedelta, timezone

        home, _, manager, _ = sandbox
        run_id, task_id = start_admitted_session(sandbox)
        store = journal.journal(home)
        feed = journal.task_feed(manager)
        future = datetime.now(timezone.utc) + timedelta(days=1)
        assert import_source_events(store, "sandbox", feed, not_before=future) == 0
        assert store.inbox() == []
        assert store.cursor("task-log:sandbox").position > 0, "the cursor still moved past it"
