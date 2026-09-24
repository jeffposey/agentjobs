"""The e2e server stops when the Playwright runner that started it is gone (task-515).

A runner killed from outside never tears its ``webServer`` processes down, and on
Windows they do not die with it: ``poetry run`` sits between the two and outlives the
runner, so the server's own parent says nothing. Measured 2026-09-24 -- ``taskkill /F``
on the runner left all four servers listening on the checkout's ports, and one such
orphan on 2026-09-21 turned every later e2e run in that checkout red for a quarter of an
hour. ``run_server.py`` now watches the runner, by a receipt rather than by a pid.

The end-to-end proof -- a killed runner, then the ports shown free -- is on task-515's
record, because it needs a live Playwright run. These pin the pieces it rests on.
"""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import Iterator

import pytest

from agentjobs.dispatch import pids

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
SLEEPER = "import sys, time\nsys.stdout.write('up')\nsys.stdout.flush()\ntime.sleep(120)\n"


def load_run_server() -> types.ModuleType:
    """Import the Playwright fixture server by path; it is not on any package path."""
    spec = importlib.util.spec_from_file_location(
        "e2e_run_server_owner", FRONTEND / "e2e" / "run_server.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["e2e_run_server_owner"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def server_module() -> types.ModuleType:
    return load_run_server()


@pytest.fixture
def runner() -> Iterator["subprocess.Popen[bytes]"]:
    """A real process standing in for the Playwright runner, killed however the test ends."""
    process = subprocess.Popen(
        [sys.executable, "-c", SLEEPER], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    assert process.stdout is not None
    process.stdout.read(2)
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=30)


class FakeServer:
    """The one attribute of ``uvicorn.Server`` the watcher touches."""

    should_exit = False


class TestTheOwnerIsRead:
    def test_run_by_hand_there_is_no_owner_to_watch(
        self, server_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(server_module.OWNER_ENV, raising=False)
        assert server_module.read_owner() is None

    def test_a_live_runner_is_recorded_with_its_receipt(
        self,
        server_module: types.ModuleType,
        runner: "subprocess.Popen[bytes]",
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The runner exists before the server it starts; here the test process plays the
        # server, and it is older than this stand-in, so pretend it was born later.
        monkeypatch.setattr(pids, "process_started_at", lambda pid: None)
        monkeypatch.setenv(server_module.OWNER_ENV, str(runner.pid))
        owner = server_module.read_owner()
        assert owner.pid == runner.pid
        assert owner.identity == pids.process_identity(runner.pid)

    def test_a_runner_already_gone_is_refused(
        self,
        server_module: types.ModuleType,
        runner: "subprocess.Popen[bytes]",
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner.kill()
        runner.wait(timeout=30)
        monkeypatch.setattr(pids, "process_alive", lambda pid: False)
        monkeypatch.setenv(server_module.OWNER_ENV, str(runner.pid))
        with pytest.raises(SystemExit, match="already gone"):
            server_module.read_owner()

    def test_a_pid_held_by_a_process_younger_than_the_server_is_refused(
        self,
        server_module: types.ModuleType,
        runner: "subprocess.Popen[bytes]",
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The runner started the server, so a holder created after the server is a stranger."""
        monkeypatch.setenv(server_module.OWNER_ENV, str(runner.pid))
        # This test process is older than `runner`, which is exactly the shape of a
        # stranger that took a dead runner's number after the server started.
        with pytest.raises(SystemExit, match="already gone"):
            server_module.read_owner()

    def test_a_nonsense_owner_is_refused(
        self, server_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(server_module.OWNER_ENV, "not-a-pid")
        with pytest.raises(SystemExit):
            server_module.read_owner()


class TestGoneMeansProvablyGone:
    def test_the_live_runner_is_not_gone(
        self, server_module: types.ModuleType, runner: "subprocess.Popen[bytes]"
    ) -> None:
        owner = server_module.Owner(runner.pid, pids.process_identity(runner.pid))
        assert server_module.owner_gone(owner) is False

    def test_an_ended_runner_is_gone(
        self, server_module: types.ModuleType, runner: "subprocess.Popen[bytes]"
    ) -> None:
        owner = server_module.Owner(runner.pid, pids.process_identity(runner.pid))
        runner.kill()
        runner.wait(timeout=30)
        assert server_module.owner_gone(owner) is True

    def test_a_stranger_holding_the_number_is_not_the_runner(
        self, server_module: types.ModuleType, runner: "subprocess.Popen[bytes]"
    ) -> None:
        """A recycled pid must not keep an orphan serving for its new holder."""
        owner = server_module.Owner(runner.pid, "win:0:not-this-process")
        assert server_module.owner_gone(owner) is True

    def test_an_unreadable_receipt_is_not_evidence(
        self,
        server_module: types.ModuleType,
        runner: "subprocess.Popen[bytes]",
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A wrong 'gone' costs a red e2e stage, so doubt keeps serving."""
        owner = server_module.Owner(runner.pid, pids.process_identity(runner.pid))
        monkeypatch.setattr(pids, "process_identity", lambda pid: None)
        assert server_module.owner_gone(owner) is False


class TestTheWatcherStopsTheServer:
    def test_the_server_is_told_to_stop_when_the_runner_ends(
        self,
        server_module: types.ModuleType,
        runner: "subprocess.Popen[bytes]",
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        exits: list[int] = []
        monkeypatch.setattr(server_module.os, "_exit", exits.append)
        server = FakeServer()
        owner = server_module.Owner(runner.pid, pids.process_identity(runner.pid))
        server_module.watch_owner(owner, server, poll=0.05, grace=0.2)
        time.sleep(0.3)
        assert server.should_exit is False, "stopped while its runner was alive"

        runner.kill()
        runner.wait(timeout=30)
        deadline = time.monotonic() + 10
        while not exits and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.should_exit is True
        assert exits == [3], "the hard exit is the backstop when a graceful stop hangs"

    def test_a_dead_stdout_does_not_stop_the_stop(
        self,
        server_module: types.ModuleType,
        runner: "subprocess.Popen[bytes]",
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The first version printed first; stdout was a pipe to the dead runner, the
        print raised, and the thread died before the stop -- all four servers stayed up."""

        class BrokenPipe(io.StringIO):
            def write(self, text: str) -> int:
                raise OSError(22, "Invalid argument")

        monkeypatch.setattr(sys, "stdout", BrokenPipe())
        exits: list[int] = []
        monkeypatch.setattr(server_module.os, "_exit", exits.append)
        server = FakeServer()
        owner = server_module.Owner(runner.pid, pids.process_identity(runner.pid))
        runner.kill()
        runner.wait(timeout=30)
        thread = server_module.watch_owner(owner, server, poll=0.05, grace=0.05)
        thread.join(timeout=10)
        assert server.should_exit is True
        assert exits == [3]


def test_the_config_names_the_runner_to_every_server() -> None:
    source = (FRONTEND / "playwright.config.ts").read_text(encoding="utf-8")
    assert 'OWNER_ENV = "AGENTJOBS_E2E_OWNER_PID"' in source
    assert "[OWNER_ENV]: String(process.pid)" in source
    assert load_run_server().OWNER_ENV == "AGENTJOBS_E2E_OWNER_PID"
