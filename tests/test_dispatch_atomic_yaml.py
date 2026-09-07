"""A run's meta.yaml is read by one process while another writes it (task-390).

Every guard in the dispatch subsystem is a ``meta.get(...)`` on a file somebody else owns:
is this run live, was a cancellation requested, is an escalation waiting on it. Until this
module existed the write was a truncate-then-rewrite and the read reported *any* failure
as an empty mapping -- so a reader landing in the window did not get an error it could
retry, it got a confident **no**.

What that cost, in the gate rather than in theory:
``test_dispatch_api.py::TestDispatchRuns::test_cancelling_a_live_run_stops_it_and_marks_it
_cancelled`` failing under xdist with ``assert 'failed' == 'cancelled'``. A cancelled batch
run's supervisor wakes to a non-zero exit, reads the file to ask whether the kill it woke
from was a cancellation, is told no, and writes ``failed`` over the cancellation.
``runner._finish_batch`` documents that race and orders its writes to remove it; the order
is right and a torn read defeats it anyway.

**The concurrency cases below are one-sided on purpose.** They assert that nothing torn
was observed, so contention -- an xdist worker, a loaded machine -- can only make them
catch more. There is no timing they need in order to pass, which is the property a test
about a race has to have if it is not to become the next flake.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Dict, List

import pytest
import yaml

from agentjobs.dispatch import atomic_yaml
from agentjobs.dispatch.atomic_yaml import read_yaml_resiliently, write_yaml_atomically
from agentjobs.dispatch.runner import RunDirectory

WRITES = 60
READERS = 3
READ_PAUSE_SECONDS = 0.0005
"""Enough to open the window many times over, and cheap enough to live in the gate.

Detection power is not delicate here: before the fix this shape produced **0 complete
reads out of 2886**, so any handful of samples catches it. The pause is what keeps the
cost down -- readers spinning with no pause are pure-Python YAML parsing on every
iteration, which starves the writer of the GIL and turned this one case into 91 seconds
without testing anything the pause does not.
"""

PAD = "y" * 4000
"""A document big enough that writing it is not one memory-page instant."""


class TestTheDocumentIsNeverHalfWritten:
    def test_a_reader_never_sees_a_partial_document(self, tmp_path: Path) -> None:
        """The measurement this was found by, as an assertion.

        Before the fix, the same shape gave **0 complete reads out of 2886**: 1085 empty
        and 1796 parsed-but-missing-keys. A truncated YAML mapping is frequently still
        valid YAML, which is why "did it parse" was never the question.
        """
        directory = RunDirectory(tmp_path)
        directory.write_meta({"run_id": "run_x", "status": "running", "pad": PAD})

        done = threading.Event()
        torn: List[Dict[str, object]] = []
        lock = threading.Lock()

        def write() -> None:
            try:
                for index in range(WRITES):
                    directory.write_meta(
                        {"run_id": "run_x", "status": "running", "pad": PAD, "n": index}
                    )
            finally:
                done.set()

        reads = [0]

        def read() -> None:
            while not done.is_set():
                meta = directory.read_meta()
                with lock:
                    reads[0] += 1
                    if "run_id" not in meta:
                        torn.append(meta)
                time.sleep(READ_PAUSE_SECONDS)

        threads = [threading.Thread(target=write)]
        threads += [threading.Thread(target=read, daemon=True) for _ in range(READERS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)

        assert reads[0] > WRITES, "the readers must have sampled across the writes"
        assert torn == [], (
            f"{len(torn)} of {reads[0]} reads saw a document without `run_id`, which no "
            "writer ever wrote. A guard reading this file would have been told a flag "
            "was absent."
        )

    def test_a_read_refused_for_an_instant_is_not_reported_as_absence(self, tmp_path: Path) -> None:
        """The second half, and the one the write fix alone left behind.

        Replacing the file removes torn content and leaves a window in which opening it
        can be refused -- on Windows, ``ERROR_ACCESS_DENIED`` while ``MoveFileExW`` swaps
        the target. ``PermissionError`` is an ``OSError``, and the readers here answered
        ``{}`` to those. Measured after the write fix and before this one: 1 to 3 reads in
        ~3700 still came back empty, none of them from an empty file.
        """
        target = tmp_path / "meta.yaml"
        write_yaml_atomically(target, {"present": True})
        attempts = {"n": 0}
        real_read_shared = atomic_yaml.read_shared

        def refuse_once(path: Path) -> str:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise PermissionError("the file is being replaced")
            return real_read_shared(path)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(atomic_yaml, "read_shared", refuse_once)
            loaded = read_yaml_resiliently(target)

        assert attempts["n"] == 2, "the refusal must be retried, not believed"
        assert loaded == {"present": True}

    def test_a_file_that_is_genuinely_missing_answers_at_once(self, tmp_path: Path) -> None:
        """Absence is not transient, and paying the retry budget for it would be a tax.

        ``None`` rather than ``{}`` so the two are distinguishable at all -- which is the
        distinction the whole module exists to restore.
        """
        assert read_yaml_resiliently(tmp_path / "nothing.yaml") is None

    def test_unparseable_content_is_not_retried(self, tmp_path: Path) -> None:
        """With the write side replacing, bad content is corrupt rather than half-written."""
        target = tmp_path / "meta.yaml"
        target.write_text("{ this is not: [valid", encoding="utf-8")

        assert read_yaml_resiliently(target) is None

    def test_no_temporary_file_is_left_beside_the_target(self, tmp_path: Path) -> None:
        """A run directory is listed by the runs API and read by ``finish_status``.

        A leaked `.tmp` outlives the run it belongs to, and there is nothing later that
        would clean one up.
        """
        target = tmp_path / "meta.yaml"
        for index in range(5):
            write_yaml_atomically(target, {"n": index})

        assert [path.name for path in tmp_path.iterdir()] == ["meta.yaml"]
        assert yaml.safe_load(target.read_text(encoding="utf-8")) == {"n": 4}

    def test_a_target_held_open_for_a_moment_is_waited_out_rather_than_failed(
        self, tmp_path: Path
    ) -> None:
        """What the retry loop is actually for, on the platform that needs it.

        On Windows ``os.replace`` is refused while any handle is open on the target --
        Python's ``open`` does not ask for ``FILE_SHARE_DELETE`` -- so a reader mid-read
        is contention rather than a coexisting reader, and the writer's job is to wait
        for it. This is the one property the loop has to have; a writer that gave up here
        would leave the record un-updated, which for ``cancel_requested`` means a
        cancellation nobody can see.
        """
        target = tmp_path / "meta.yaml"
        write_yaml_atomically(target, {"generation": 1, "pad": PAD})
        release = threading.Event()
        opened = threading.Event()

        def hold() -> None:
            with target.open(encoding="utf-8"):
                opened.set()
                release.wait(timeout=30)

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        assert opened.wait(timeout=10)
        threading.Timer(0.15, release.set).start()

        write_yaml_atomically(target, {"generation": 2, "pad": PAD})
        holder.join(timeout=10)

        assert yaml.safe_load(target.read_text(encoding="utf-8"))["generation"] == 2
