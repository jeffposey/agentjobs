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
from agentjobs.execution.store import PROVENANCE_NATIVE, Attempt

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
        self, tmp_path: Path, pid: Optional[int], admitted_at: str
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
