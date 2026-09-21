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
from typing import Iterator

import pytest

from agentjobs.dispatch.pids import (
    is_the_recorded_process,
    process_alive,
    process_created_after,
    process_identity,
    recorded_process_alive,
)
from agentjobs.dispatch.runner import _kill_tree

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


def moment_now() -> datetime:
    return datetime.now(timezone.utc)


def os_pid() -> int:
    import os

    return os.getpid()
