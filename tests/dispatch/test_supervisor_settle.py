"""A run's terminal entry survives the shutdown of whatever was serving it (task-505).

A batch run's supervisor is a thread, and it is the **only** writer of that run's
``dispatch_result``: it writes it after the worker exits. Everything that closes the
SQLite stores -- the served application's lifespan, and every test whose ``TestClient``
leaves it -- was closing them under live supervisors, so that write raised
``sqlite3.ProgrammingError: Cannot operate on a closed database`` inside a thread nobody
awaits. The run's one terminal entry was lost and the only trace was a warning. Thirteen
of them in ``tests/test_dispatch_api.py`` alone, and the same exception is what floods
the e2e server log.

The fix is a bounded wait, and bounded is the whole design: a supervisor may be watching
a worker that legitimately runs for an hour, so a shutdown that waited for it would never
be a shutdown.
"""

from __future__ import annotations

import threading
import time

from agentjobs.dispatch.runner import SUPERVISOR_THREAD_PREFIX, settle_supervisors


def _supervisor(release: threading.Event, done: list[str], name: str) -> threading.Thread:
    def body() -> None:
        release.wait(timeout=60)
        done.append(name)

    thread = threading.Thread(target=body, name=name, daemon=True)
    thread.start()
    return thread


class TestSettleSupervisors:
    def test_it_waits_for_a_supervisor_to_finish_its_write(self) -> None:
        release = threading.Event()
        done: list[str] = []
        name = f"{SUPERVISOR_THREAD_PREFIX}run_settle01"
        thread = _supervisor(release, done, name)
        try:
            release.set()
            settle_supervisors(grace=30.0)
            assert done == [name]
            assert not thread.is_alive()
        finally:
            release.set()
            thread.join(timeout=30)

    def test_a_supervisor_that_does_not_finish_bounds_the_wait(self) -> None:
        """A worker may legitimately outlive the process watching it. Nothing hangs."""
        release = threading.Event()
        done: list[str] = []
        name = f"{SUPERVISOR_THREAD_PREFIX}run_settle02"
        thread = _supervisor(release, done, name)
        try:
            started = time.monotonic()
            settle_supervisors(grace=0.5)
            waited = time.monotonic() - started
            assert waited < 10.0
            assert thread.is_alive()
        finally:
            release.set()
            thread.join(timeout=30)

    def test_it_waits_for_nothing_else(self) -> None:
        """Only a run's supervisor. Every other thread in the process is somebody else's."""
        release = threading.Event()
        done: list[str] = []
        thread = _supervisor(release, done, "some-other-worker")
        try:
            started = time.monotonic()
            settle_supervisors(grace=30.0)
            assert time.monotonic() - started < 10.0
            assert thread.is_alive()
        finally:
            release.set()
            thread.join(timeout=30)


class TestTheApplicationSettlesBeforeItCloses:
    def test_a_shutdown_does_not_leave_a_supervisor_mid_write(self) -> None:
        """The served path, not a unit of it: leaving the lifespan waits for the thread.

        The supervisor is released four tenths of a second after the application is up,
        so an unsettled shutdown returns while it is still blocked and ``done`` is empty.
        That is exactly what a server restart under a live batch run used to do.
        """
        from starlette.testclient import TestClient

        from agentjobs.api.main import app

        release = threading.Event()
        done: list[str] = []
        name = f"{SUPERVISOR_THREAD_PREFIX}run_lifespan1"
        thread = _supervisor(release, done, name)
        try:
            with TestClient(app):
                threading.Timer(0.4, release.set).start()
            assert done == [name]
            assert not thread.is_alive()
        finally:
            release.set()
            thread.join(timeout=30)
