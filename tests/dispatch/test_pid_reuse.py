"""A recorded pid is not a name for a process, and acting on one as if it were (task-505).

This machine hands a dead process's number to a new process within seconds: 2000
short-lived children spawned eight at a time on 2026-09-21 used 988 distinct pids, with a
median gap of 23.7 seconds between a number dying and being reissued and a minimum of
0.35. Every test here spawns a **real** stranger and proves what the code does with its
number, because the defect was precisely that a stranger was indistinguishable from the
recorded process.

The two failures that produced are opposite and both are exercised below: believing a
stranger is the recorded process (so a run is never concluded), and killing one (so an
unrelated process dies with an exit code its own parent then has to explain).
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional, Tuple

import pytest

from agentjobs.dispatch import pids
from agentjobs.dispatch.journal import attempt_evidence
from agentjobs.dispatch.pids import (
    describe_exit,
    is_the_recorded_process,
    process_alive,
    process_created_after,
    process_identity,
    recorded_process_alive,
)
from agentjobs.dispatch.runner import _kill_tree
from agentjobs.execution.errors import OwnershipConflict
from agentjobs.execution.store import PROVENANCE_NATIVE, Attempt, ExecutionStore, Supervision

SLEEPER = "import sys, time\nsys.stdout.write('up')\nsys.stdout.flush()\ntime.sleep(120)\n"
"""A child that announces itself and then does nothing, so a test can wait for it to be
running rather than sleeping and hoping."""


@pytest.fixture
def stranger() -> Iterator["subprocess.Popen[bytes]"]:
    """A real process nothing under test started, killed however the test ends."""
    process = subprocess.Popen(
        [sys.executable, "-c", SLEEPER], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    assert process.stdout is not None
    process.stdout.read(2)  # it is running once it has said so
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=30)


class TestTheReceipts:
    def test_an_identity_names_one_process_and_not_its_number(
        self, stranger: "subprocess.Popen[bytes]"
    ) -> None:
        receipt = process_identity(stranger.pid)
        assert receipt is not None
        assert process_identity(stranger.pid) == receipt  # stable while it lives
        assert receipt != process_identity(os_pid())

    def test_a_pid_nothing_holds_has_no_identity_and_no_start_time(self) -> None:
        """The impossible pid every caller has to survive being handed."""
        assert process_identity(0) is None
        assert process_alive(0) is False
        assert process_created_after(0, moment_now()) is False

    def test_a_process_started_after_the_record_is_proof_of_reuse(
        self, stranger: "subprocess.Popen[bytes]"
    ) -> None:
        before = moment_now() - timedelta(minutes=5)
        assert process_created_after(stranger.pid, before) is True
        assert process_created_after(stranger.pid, moment_now()) is False

    def test_a_start_time_is_the_moment_a_younger_process_is_judged_against(
        self, stranger: "subprocess.Popen[bytes]"
    ) -> None:
        """This process is older than the stranger, so the stranger reads as created after."""
        born = pids.process_started_at(os_pid())
        assert born is not None and born <= moment_now()
        assert process_created_after(stranger.pid, born) is True
        assert pids.process_started_at(0) is None


class TestLivenessAnswersYesOnDoubtAndNoOnEvidence:
    def test_a_stranger_at_a_recorded_pid_is_not_the_recorded_process(
        self, stranger: "subprocess.Popen[bytes]"
    ) -> None:
        """The false-alive half. Before task-505 this was a bare ``process_alive``."""
        recorded_long_ago = moment_now() - timedelta(minutes=5)
        assert process_alive(stranger.pid) is True
        assert recorded_process_alive(stranger.pid, recorded_at=recorded_long_ago) is False
        assert recorded_process_alive(stranger.pid, identity="win:1:1") is False

    def test_the_recorded_process_itself_still_reads_as_running(
        self, stranger: "subprocess.Popen[bytes]"
    ) -> None:
        receipt = process_identity(stranger.pid)
        assert recorded_process_alive(stranger.pid, identity=receipt) is True
        assert recorded_process_alive(stranger.pid, recorded_at=moment_now()) is True

    def test_no_receipt_at_all_is_the_old_answer(self, stranger: "subprocess.Popen[bytes]") -> None:
        """Doubt answers yes here: refusing to conclude a live run is the recoverable error."""
        assert recorded_process_alive(stranger.pid) is True
        assert recorded_process_alive(None) is False


class TestNothingIsKilledOnAPidAlone:
    def test_a_kill_with_no_receipt_kills_nothing(
        self, stranger: "subprocess.Popen[bytes]"
    ) -> None:
        """A bare pid is not authority. Doubt answers *no* on this side of the rule."""
        assert _kill_tree(stranger.pid) is False
        assert stranger.poll() is None

    def test_a_kill_on_a_wrong_receipt_kills_nothing(
        self, stranger: "subprocess.Popen[bytes]"
    ) -> None:
        assert _kill_tree(stranger.pid, identity="win:1:1") is False
        assert _kill_tree(stranger.pid, recorded_at=moment_now() - timedelta(minutes=5)) is False
        assert stranger.poll() is None

    def test_a_kill_on_the_right_receipt_kills_it_and_leaves_exit_1_behind(
        self, stranger: "subprocess.Popen[bytes]"
    ) -> None:
        """And this is the signature task-505 was filed about, recorded deliberately.

        A process killed this way exits 1 with nothing on either stream -- which no
        Python traceback and no failed assertion can produce, and which is what an
        unrelated test saw when a recycled pid was killed out from under it.
        """
        receipt = process_identity(stranger.pid)
        assert _kill_tree(stranger.pid, identity=receipt) is True
        assert stranger.wait(timeout=60) == 1
        assert stranger.stdout is not None and stranger.stdout.read() == b""
        assert stranger.stderr is not None and stranger.stderr.read() == b""

    def test_a_caller_holding_the_handle_needs_no_receipt(
        self, stranger: "subprocess.Popen[bytes]"
    ) -> None:
        """An open OS handle is what stops the number being reused, so it is its own proof."""
        assert _kill_tree(stranger.pid, holding_handle=True) is True
        assert stranger.wait(timeout=60) == 1

    def test_a_pid_that_is_gone_is_refused_rather_than_aimed_at(
        self, stranger: "subprocess.Popen[bytes]"
    ) -> None:
        """The window the whole rule is about: the receipt outlives the process."""
        receipt = process_identity(stranger.pid)
        stranger.kill()
        stranger.wait(timeout=60)
        deadline = time.monotonic() + 30
        while process_alive(stranger.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _kill_tree(stranger.pid, identity=receipt) is False
        assert is_the_recorded_process(stranger.pid, identity=receipt) is False


ORPHANING = (
    "import subprocess, sys\n"
    "quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], **quiet)\n"
    "print(child.pid, flush=True)\n"
)
"""Starts a sleeper and exits, so the test holds no handle on the sleeper -- as the
process that kills a recorded worker holds none on it. The sleeper's streams are its own,
or reading this script's output would wait the full two minutes for it."""


@pytest.mark.skipif(sys.platform != "win32", reason="a handle reserving a pid is Windows")
class TestTheProofIsHeldAcrossTheKill:
    """task-554: a proof passed *before* the kill is not a proof *at* the kill.

    ``_kill_tree`` used to prove the receipt and then spawn ``taskkill /PID <number>``.
    A worker that exited on its own during that spawn freed its number, and this machine
    reissues a number in 0.35s, so ``taskkill`` could land on whatever was handed it.
    Nothing here has to win a race to show that. The target is ended inside the window,
    deterministically, and the assertion is about the **number**: while the proof is held
    it still names the recorded process, so there is nothing a stranger could be holding.
    """

    def test_a_target_that_exits_inside_the_window_keeps_its_number(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import agentjobs.dispatch.runner as runner_module

        parent = subprocess.run(
            [sys.executable, "-c", ORPHANING], capture_output=True, text=True, timeout=120
        )
        target = int(parent.stdout.split()[-1])
        receipt = process_identity(target)
        assert receipt is not None, "the sleeper was not running, so this proves nothing"
        seen: list = []

        def exits_first(pid: int) -> bool:
            # The worker ends by itself inside the window, the way a run does when its
            # work finishes as the cancel arrives...
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
            deadline = time.monotonic() + 30
            while process_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            assert not process_alive(pid), "the target never ended"
            # ...and its number is still its own: the corpse, reserved, not a vacancy.
            seen.append(process_identity(pid))
            return True

        monkeypatch.setattr(runner_module, "_kill_tree_now", exits_first)
        assert _kill_tree(target, identity=receipt) is True
        assert seen == [receipt], (
            "the number was released while the proof was being acted on; a stranger "
            "given it in that window would have been the one killed"
        )

    def test_once_the_kill_is_over_the_number_is_released(self) -> None:
        """The handle is not leaked: nothing reserves a pid past the call that proved it."""
        parent = subprocess.run(
            [sys.executable, "-c", ORPHANING], capture_output=True, text=True, timeout=120
        )
        target = int(parent.stdout.split()[-1])
        receipt = process_identity(target)
        assert receipt is not None
        assert _kill_tree(target, identity=receipt) is True
        deadline = time.monotonic() + 30
        while process_identity(target) == receipt and time.monotonic() < deadline:
            time.sleep(0.05)
        assert process_identity(target) != receipt


class TestANeverLaunchedAttemptIsReleasedPastAReusedPid:
    """``attempt_evidence``'s never-launched branch, with the reused-pid state built directly.

    task-489: a launcher crashed before writing its launch marker, its pid was reissued
    before the next tick, and a bare ``process_alive`` kept the attempt owned -- which is
    what ``test_a_fresh_process_performs_the_recovery`` caught at random under a loaded
    gate. Racing the kernel for a recycled number would measure the machine, so these
    admit an attempt on a live stranger's pid and set ``admitted_at`` either side of when
    that stranger started.
    """

    NEVER_LAUNCHED = "admitted but never launched"

    def verdict(
        self,
        tmp_path: Path,
        pid: Optional[int],
        admitted_at: str,
        holder_identity: Optional[str] = None,
    ) -> Optional[Tuple[str, str, str]]:
        # No run directory, so no session, no pid and no launch marker: the only thing
        # standing between this attempt and release is whether its holder is running.
        return attempt_evidence(tmp_path, lambda _project: None)(
            Attempt(
                run_id="run_never_launched",
                execution_id=None,
                project_id="p",
                task_id="task-1",
                mode="session",
                takes_slot=True,
                holder="launcher",
                holder_pid=pid,
                epoch=1,
                owner_mode="dispatch",
                provenance=PROVENANCE_NATIVE,
                state="admitted",
                reservation="none",
                reservation_data={},
                cancel_requested=False,
                cancel=None,
                control_generation=0,
                session_id=None,
                outcome=None,
                status=None,
                concluded_by=None,
                admitted_at=admitted_at,
                launched_at=None,
                concluded_at=None,
                holder_identity=holder_identity,
            )
        )

    def test_a_stranger_started_after_the_admission_does_not_keep_it_owned(
        self, tmp_path: Path, stranger: "subprocess.Popen[bytes]"
    ) -> None:
        """The defect. The pid is alive; the process holding it is not the admitter."""
        admitted = (moment_now() - timedelta(minutes=5)).isoformat()
        assert process_alive(stranger.pid) is True
        released = self.verdict(tmp_path, stranger.pid, admitted)
        assert released is not None and self.NEVER_LAUNCHED in released[2]

    def test_the_admitter_itself_still_running_keeps_it_owned(
        self, tmp_path: Path, stranger: "subprocess.Popen[bytes]"
    ) -> None:
        """A holder admitted *after* it started is the holder, so nothing is released."""
        assert self.verdict(tmp_path, stranger.pid, moment_now().isoformat()) is None

    def test_a_holder_that_is_gone_releases_it(self, tmp_path: Path) -> None:
        released = self.verdict(tmp_path, 0, moment_now().isoformat())
        assert released is not None and self.NEVER_LAUNCHED in released[2]

    def test_an_unreadable_start_time_is_not_proof_of_reuse(
        self,
        tmp_path: Path,
        stranger: "subprocess.Popen[bytes]",
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Doubt keeps ownership: releasing a possibly-live worker is task-419's failure."""
        monkeypatch.setattr(pids, "_times", lambda _pid: None)
        admitted = (moment_now() - timedelta(minutes=5)).isoformat()
        assert self.verdict(tmp_path, stranger.pid, admitted) is None

    def test_an_unreadable_admission_time_is_not_proof_of_reuse(
        self, tmp_path: Path, stranger: "subprocess.Popen[bytes]"
    ) -> None:
        assert self.verdict(tmp_path, stranger.pid, "not a timestamp") is None

    def test_the_admitters_receipt_keeps_it_owned_whatever_the_journal_clock_said(
        self, tmp_path: Path, stranger: "subprocess.Popen[bytes]"
    ) -> None:
        """task-549. The journal writes ``admitted_at`` through the installed clock, which
        a test freezes before the admitting process even exists. Compared with the OS's
        start time, a live admitter read as a recycled pid, its attempt was released while
        it was launching, and a second dispatch took the slot it still held."""
        receipt = process_identity(stranger.pid)
        before_it_started = (moment_now() - timedelta(minutes=5)).isoformat()
        assert self.verdict(tmp_path, stranger.pid, before_it_started, receipt) is None

    def test_a_receipt_that_does_not_match_releases_it_whatever_the_clock_said(
        self, tmp_path: Path, stranger: "subprocess.Popen[bytes]"
    ) -> None:
        """The receipt is the proof in both directions, not only the forgiving one."""
        released = self.verdict(tmp_path, stranger.pid, moment_now().isoformat(), "win:1:1")
        assert released is not None and self.NEVER_LAUNCHED in released[2]

    def test_an_unreadable_receipt_now_is_not_proof_of_reuse(
        self,
        tmp_path: Path,
        stranger: "subprocess.Popen[bytes]",
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        receipt = process_identity(stranger.pid)
        monkeypatch.setattr(pids, "_times", lambda _pid: None)
        assert self.verdict(tmp_path, stranger.pid, moment_now().isoformat(), receipt) is None


def moment_now() -> datetime:
    return datetime.now(timezone.utc)


def os_pid() -> int:
    import os

    return os.getpid()


class TestTheVictimsEndIsSaidOutLoud:
    """The same mechanism read from the victim's end: what the message says (task-513).

    Every test above is about not killing the wrong process. These are about the report
    that reaches a human when something already did. ``Session launch for task-001
    exited 1:`` -- with nothing after the colon -- was the whole of what one gate run
    recorded about a real ``claude`` that never started, and an agent spent a detour
    proving it was not a code defect.
    """

    def test_a_really_killed_child_is_described_as_killed_not_as_silent(
        self, stranger: "subprocess.Popen[bytes]"
    ) -> None:
        """Built from what a real kill leaves behind, never from a hand-made record.

        A constructed ``CompletedProcess`` would prove only that the function reads its
        own arguments. Killing a real process and describing what the OS actually left
        is what ties the sentence to the thing it claims to recognise.
        """
        receipt = process_identity(stranger.pid)
        assert _kill_tree(stranger.pid, identity=receipt) is True
        assert stranger.wait(timeout=60) == 1
        assert stranger.stdout is not None and stranger.stderr is not None
        completed = subprocess.CompletedProcess(
            args=["claude", "--bg"],
            returncode=stranger.returncode,
            stdout=stranger.stdout.read().decode(),
            stderr=stranger.stderr.read().decode(),
        )

        described = describe_exit(completed)
        assert "exit 1" in described
        assert "taskkill" in described, "the reading that saves the detour"
        assert "task-505" in described, "and where to go and read about it"
        assert described.count("<empty>") == 2, "both streams named rather than omitted"

    def test_an_ordinary_failure_is_not_blamed_on_a_kill(self) -> None:
        """The error that would matter most: sending the next reader after a phantom pid.

        A CLI that exits 1 *and says why* is the common case by far, so the kill reading
        has to stay off it. Only the pair -- exit 1 and nothing on either stream --
        earns the sentence.
        """
        spoke = subprocess.run(
            [sys.executable, "-c", "import sys; sys.stderr.write('bad flag\n'); sys.exit(1)"],
            capture_output=True,
            text=True,
        )
        described = describe_exit(spoke)
        assert "bad flag" in described
        assert "taskkill" not in described

    def test_a_nonzero_code_that_is_not_one_is_not_blamed_on_a_kill_either(self) -> None:
        """Exit 9 with two empty streams is a child that chose to say nothing."""
        quiet = subprocess.run([sys.executable, "-c", "raise SystemExit(9)"], capture_output=True)
        described = describe_exit(
            subprocess.CompletedProcess(
                args=["x"], returncode=quiet.returncode, stdout="", stderr=""
            )
        )
        assert "exit 9" in described
        assert "taskkill" not in described


class TestAWalkIsNotRefusedByAStrangerOnItsDeadSupervisorsPid:
    """task-558. One supervisor per epic is a lease on ``supervision``, and the takeover
    rule asked whether the process at ``holder_pid`` was created after ``updated_at``. A
    supervisor that wrote to its walk and died inside a second, whose number was reissued
    inside the same second -- 0.35 s is this machine's measured minimum -- left a stranger
    that ``REUSE_SLACK`` could not tell from the holder, and every later walk of the epic
    refused ALREADY_SUPERVISED on a process that had never walked anything."""

    def open(
        self,
        tmp_path: Path,
        stamp: datetime,
        holder: str,
        pid: int,
        **kwargs: object,
    ) -> Tuple[Supervision, bool]:
        store = ExecutionStore(tmp_path / "execution.db", clock=lambda: stamp)
        try:
            return store.open_walk(
                project_id="sandbox",
                parent_task_id="task-001",
                authority_entry=7,
                authority_actor="Jeff Posey",
                settings={},
                holder=holder,
                holder_pid=pid,
                holder_alive=process_alive,
                **kwargs,  # type: ignore[arg-type]
            )
        finally:
            store.close()

    def test_a_stranger_born_inside_the_slack_does_not_hold_the_epic(
        self, tmp_path: Path, stranger: "subprocess.Popen[bytes]"
    ) -> None:
        born = pids.process_started_at(stranger.pid)
        assert born is not None
        # The dead supervisor's last write, half a second before its number was reissued.
        last_write = born - timedelta(seconds=0.5)
        first, _ = self.open(
            tmp_path, last_write, "gone:1", stranger.pid, holder_identity=f"gone:{stranger.pid}:1"
        )
        second, resumed = self.open(tmp_path, moment_now(), "fresh:2", os_pid())
        assert resumed and second.walk_id == first.walk_id and second.holder == "fresh:2"

    def test_a_live_holder_still_refuses_a_second_supervisor(
        self, tmp_path: Path, stranger: "subprocess.Popen[bytes]"
    ) -> None:
        """The rule the fix must not relax: two live supervisors never walk one epic."""
        self.open(tmp_path, moment_now(), "alive:1", stranger.pid)
        with pytest.raises(OwnershipConflict, match="already being walked"):
            self.open(tmp_path, moment_now(), "fresh:2", os_pid())

    def test_a_live_holder_refuses_whatever_its_clock_said(
        self, tmp_path: Path, stranger: "subprocess.Popen[bytes]"
    ) -> None:
        """The opposite skew, task-549's: a stamp from before the holder started made a live
        holder read as recycled. Its receipt keeps it the holder."""
        self.open(tmp_path, moment_now() - timedelta(minutes=5), "alive:1", stranger.pid)
        with pytest.raises(OwnershipConflict, match="already being walked"):
            self.open(tmp_path, moment_now(), "fresh:2", os_pid())

    def test_a_row_without_a_receipt_keeps_the_timestamp_check(
        self, tmp_path: Path, stranger: "subprocess.Popen[bytes]"
    ) -> None:
        """A row an earlier build wrote has no receipt; a stranger created well after it is
        still recognised by the moment, as before."""
        self.open(
            tmp_path,
            moment_now() - timedelta(minutes=5),
            "old:1",
            stranger.pid,
            holder_identity=None,
        )
        second, resumed = self.open(tmp_path, moment_now(), "fresh:2", os_pid())
        assert resumed and second.holder == "fresh:2"
