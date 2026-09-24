"""Every kill AgentJobs makes names itself in the kill journal (task-561).

Flake register row 13 -- a sibling dispatch exits 1 with both streams empty -- fires too
rarely to reproduce, so its victim has to be able to ask, after the fact, whether
AgentJobs killed it. These tests kill a **real** stranger through each kill site and read
the line it left, and pin the journal's three promises: it never fails a kill, it stays
bounded, and a reused pid from before the victim existed does not incriminate anybody.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest

from agentjobs import cli
from agentjobs.dispatch import kills, proctree
from agentjobs.dispatch.pids import process_identity
from agentjobs.dispatch.runner import _kill_tree

SLEEPER = "import sys, time\nsys.stdout.write('up')\nsys.stdout.flush()\ntime.sleep(120)\n"


@pytest.fixture
def journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """This test's own journal, so nothing another test killed shows up in it."""
    path = tmp_path / "kills.jsonl"
    monkeypatch.setenv(kills.JOURNAL_ENV, str(path))
    return path


@pytest.fixture
def stranger() -> Iterator["subprocess.Popen[bytes]"]:
    """A real process nothing under test started, reaped however the test ends."""
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


def lines_for(site: str, pid: int) -> List[Dict[str, Any]]:
    return [line for line in kills.read() if line["site"] == site and line["target"]["pid"] == pid]


def assert_journalled(site: str, process: "subprocess.Popen[bytes]", identity: str) -> None:
    before, after = lines_for(site, process.pid)
    assert before["phase"] == "kill" and after["phase"] == "ended"
    for line in (before, after):
        assert line["target"]["identity"] == identity
        assert line["caller"]["pid"] == os.getpid()
        assert line["caller"]["identity"] == process_identity(os.getpid())
        assert line["caller"]["command"]
    assert process.pid in after["ended"], after
    assert kills.naming([process.pid]) == [before, after]


class TestEverySiteJournals:
    def test_runner_kill_tree_names_everything_taskkill_ended(
        self, journal: Path, stranger: "subprocess.Popen[bytes]"
    ) -> None:
        identity = process_identity(stranger.pid)
        assert identity is not None
        assert _kill_tree(stranger.pid, identity=identity)
        stranger.wait(timeout=30)
        assert_journalled("runner._kill_tree", stranger, identity)
        ended = lines_for("runner._kill_tree", stranger.pid)[1]
        if os.name == "nt":
            # taskkill's own words, and every pid in them: a venv's python.exe is a
            # launcher, so /T ends its interpreter child as well as the pid it was given.
            assert "SUCCESS" in ended["output"]
            assert ended["returncode"] == 0
            assert len(ended["ended"]) == len(set(ended["ended"])) >= 1

    def test_a_refused_kill_journals_nothing(
        self, journal: Path, stranger: "subprocess.Popen[bytes]"
    ) -> None:
        assert not _kill_tree(stranger.pid, identity="win:1:1")
        assert stranger.poll() is None
        assert kills.read() == []

    @pytest.mark.skipif(os.name != "nt", reason="TerminateProcess is the Windows branch")
    def test_proctree_terminate(self, journal: Path, stranger: "subprocess.Popen[bytes]") -> None:
        identity = process_identity(stranger.pid)
        assert identity is not None
        created = int(identity.rsplit(":", 1)[1])
        assert proctree.terminate(proctree.Proc(pid=stranger.pid, ppid=0, created=created))
        stranger.wait(timeout=30)
        assert_journalled("proctree.terminate", stranger, identity)

    def test_cli_stop_server(
        self,
        journal: Path,
        stranger: "subprocess.Popen[bytes]",
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        identity = process_identity(stranger.pid)
        assert identity is not None
        # Nothing listens, so the stop is judged done as soon as the kill is.
        monkeypatch.setattr(cli, "_find_process_by_port", lambda _port: None)
        assert cli._stop_server(stranger.pid, port=9)
        stranger.wait(timeout=30)
        if os.name == "nt":
            assert_journalled("cli._stop_server", stranger, identity)
        else:  # pragma: no cover - Windows is the reference platform
            assert lines_for("cli._stop_server", stranger.pid)


class TestTheJournalsPromises:
    def test_a_write_that_fails_is_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A directory where the file should be: every append fails.
        monkeypatch.setenv(kills.JOURNAL_ENV, str(tmp_path))
        kills.record("runner._kill_tree", 1234)
        assert kills.read(tmp_path / "absent.jsonl") == []

    def test_it_rotates_past_the_cap_and_reads_both_halves(
        self, journal: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(kills, "CAP_BYTES", 10)
        kills.record("a", 1)
        kills.record("b", 2)
        kills.record("c", 3)
        assert journal.with_name("kills.jsonl.1").exists()
        # Two files at most: the oldest line went when the second rotation replaced .1.
        assert [line["site"] for line in kills.read()] == ["b", "c"]

    def test_a_torn_line_is_skipped(self, journal: Path) -> None:
        kills.record("a", 1)
        with journal.open("a", encoding="utf-8") as handle:
            handle.write('{"torn": \n')
        kills.record("b", 2)
        assert [line["site"] for line in kills.read()] == ["a", "b"]

    def test_the_home_is_the_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(kills.JOURNAL_ENV)
        monkeypatch.setenv("AGENTJOBS_HOME", str(tmp_path))
        assert kills.journal_path() == tmp_path / "kills.jsonl"


class TestFindingAVictim:
    def test_taskkill_output_names_only_what_it_ended(self) -> None:
        output = (
            "SUCCESS: The process with PID 812 (child process of PID 700) has been "
            "terminated.\r\nSUCCESS: The process with PID 700 (child process of PID 6) "
            "has been terminated.\r\n"
        )
        assert kills.ended_pids(output) == [812, 700]

    def test_a_pid_named_only_as_an_ended_child_is_found(self, journal: Path) -> None:
        kills.record("runner._kill_tree", 700, phase="ended", output="The process with PID 812")
        assert len(kills.naming([812])) == 1
        assert kills.naming([6]) == []

    def test_a_kill_from_before_the_victim_existed_is_not_its_killer(self, journal: Path) -> None:
        kills.record("runner._kill_tree", 812)
        later = datetime.now(timezone.utc) + timedelta(seconds=5)
        assert kills.naming([812], since=later) == []
        assert len(kills.naming([812], since=later - timedelta(seconds=10))) == 1

    def test_describe_says_so_when_nothing_in_agentjobs_did_it(self, journal: Path) -> None:
        text = kills.describe({"interpreter": 812, "launcher": 700})
        assert "no AgentJobs kill named interpreter pid 812, launcher pid 700" in text
        assert "not AgentJobs" in text

    def test_describe_quotes_the_line(self, journal: Path) -> None:
        kills.record("proctree.terminate", 812, identity="win:812:5")
        text = kills.describe({"interpreter": 812})
        [quoted] = text.splitlines()[1:]
        assert json.loads(quoted)["site"] == "proctree.terminate"
