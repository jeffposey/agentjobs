"""A stopped session takes its tree with it, and a gate says when memory was short (task-548)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, List, Optional

import pytest

from agentjobs import memory
from agentjobs.dispatch import ledger as ledger_module
from agentjobs.dispatch import proctree
from agentjobs.dispatch.finish import GateAttempt, GateVerdict, gate_memory, lead_with_the_cause
from agentjobs.dispatch.ledger import DispatchLedger, RunRecord
from agentjobs.dispatch.proctree import Proc, descendants, reap, session_roots
from agentjobs.history import GateAssembler
from agentjobs.memory import MemoryState

SESSION = "deadbeef-1111-2222-3333-444455556666"


def proc(pid: int, ppid: int, created: int, name: str = "python.exe", cmd: str = "") -> Proc:
    return Proc(pid=pid, ppid=ppid, created=created, name=name, cmdline=cmd)


def recorder(ended: List[int]) -> Any:
    def end(row: Proc) -> bool:
        ended.append(row.pid)
        return True

    return end


# ----- linking a tree ---------------------------------------------------------------


class TestDescendants:
    def test_the_whole_subtree_under_claude_is_found_and_the_roots_are_not(self) -> None:
        host = proc(10, 1, 100, "claude.exe", f"claude.exe --bg-pty-host --session-id {SESSION}")
        claude = proc(11, 10, 200, "claude.exe", f"claude.exe --session-id {SESSION}")
        table = [
            host,
            claude,
            proc(12, 10, 210, "conhost.exe"),  # the host's console, not the run's work
            proc(20, 11, 300, "bash.exe"),
            proc(21, 20, 400, "python.exe", "python scripts/check.py"),
            proc(22, 21, 500, "python.exe", "pytest -n 26"),
            proc(23, 22, 600, "python.exe", "execnet worker"),
            proc(30, 11, 310, "cmd.exe", "agentjobs mcp"),
            proc(99, 1, 50, "python.exe", "somebody else's"),
        ]
        roots = session_roots(table, "deadbeef")
        assert {row.pid for row in roots} == {10, 11}
        found = descendants(table, proctree.session_workers(roots))
        assert sorted(row.pid for row in found) == [20, 21, 22, 23, 30]

    def test_a_row_older_than_its_parent_belongs_to_an_earlier_holder_of_the_number(
        self,
    ) -> None:
        parent = proc(11, 1, 1_000_000)
        table = [parent, proc(20, 11, 10), proc(21, 11, 2_000_000)]
        assert [row.pid for row in descendants(table, [parent])] == [21]

    def test_children_of_a_recycled_parent_number_are_not_the_old_parents(self) -> None:
        dead_parent = proc(11, 1, 1_000_000)
        stranger = proc(11, 1, 9_000_000)  # the number, handed on
        table = [
            stranger,
            proc(20, 11, 2_000_000),  # the old parent's orphan
            proc(21, 11, 9_500_000),  # the stranger's child
        ]
        assert [row.pid for row in descendants(table, [dead_parent])] == [20]


class TestReap:
    def test_survivors_are_ended_and_the_roots_are_left_to_the_session_manager(self) -> None:
        claude = proc(11, 1, 200, "claude.exe", f"claude.exe --session-id {SESSION}")
        snapshot = [proc(20, 11, 300), proc(21, 20, 400)]
        later = proc(22, 21, 500)  # started after the snapshot
        ended: List[int] = []
        tables = iter(
            [
                [claude, *snapshot, later],  # claude still shutting down
                [*snapshot, later],  # it has gone
                [],
            ]
        )

        result = reap(
            snapshot,
            roots=[claude],
            table=lambda: next(tables),
            end=recorder(ended),
            sleep=lambda _: None,
        )

        assert sorted(ended) == [20, 21, 22]
        assert 11 not in ended
        assert "ended 3 process(es)" in result.sentence()

    def test_nothing_to_reap_says_so(self) -> None:
        result = reap([proc(20, 11, 300)], table=lambda: [], end=lambda row: True)
        assert result.sentence() == "it left no process behind"

    def test_an_unreadable_table_is_reported_not_raised(self) -> None:
        def broken() -> List[Proc]:
            raise OSError("CIM said no")

        result = reap([proc(20, 11, 300)], table=broken, end=lambda row: True)
        assert "CIM said no" in result.sentence()


# ----- the cancel path ---------------------------------------------------------------


class _Completed:
    returncode = 0
    stdout = ""
    stderr = ""


def a_session_record(tmp_path: Path, short_id: str) -> RunRecord:
    return RunRecord(run_id="run_x", path=tmp_path / "run_x", mode="session", session_id=short_id)


class TestStopSessionReapsItsTree:
    def test_the_stop_detail_names_what_was_ended(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        claude = proc(11, 1, 200, "claude.exe", f"claude.exe --session-id {SESSION}")
        gate = proc(21, 11, 400, "python.exe", "python scripts/check.py")
        alive = {"claude": True}

        def table() -> List[Proc]:
            return [claude, gate] if alive["claude"] else [gate]

        ended: List[int] = []
        monkeypatch.setattr(ledger_module, "SESSION_TREE_READER", table)
        monkeypatch.setattr(proctree, "terminate", recorder(ended))
        ledger = DispatchLedger(tmp_path)

        def stop(*args: str) -> Any:
            alive["claude"] = False
            return _Completed()

        monkeypatch.setattr(ledger, "_session", stop)
        result = ledger._stop_session(a_session_record(tmp_path, "deadbeef"))

        assert result.stopped
        assert ended == [21]
        assert "stopped session deadbeef; ended 1 process(es)" in result.detail

    @pytest.mark.skipif(os.name != "nt", reason="the reference platform is Windows")
    def test_a_real_tree_leaves_nothing_alive_after_the_stop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The acceptance shape of ac-2, on real processes: a stand-in session whose
        command line carries a unique ``--session-id`` starts a child that starts a
        grandchild; the "stop" ends only the stand-in, as ``claude stop`` ends only
        ``claude.exe``; nothing below it may survive."""
        session = f"{uuid.uuid4()}"
        pids = tmp_path / "pids.json"
        grandchild = "import time; time.sleep(120)"
        child = (
            "import subprocess, sys, json, time; "
            f"g = subprocess.Popen([sys.executable, '-c', {grandchild!r}]); "
            f"open({str(pids)!r}, 'w').write(json.dumps([g.pid])); time.sleep(120)"
        )
        root_code = (
            "import subprocess, sys, time; "
            f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(120)"
        )
        root = subprocess.Popen([sys.executable, "-c", root_code, "--session-id", session])
        try:
            deadline = time.monotonic() + 30
            while not pids.exists() and time.monotonic() < deadline:
                time.sleep(0.2)
            assert pids.exists(), "the stand-in tree never started"

            ledger = DispatchLedger(tmp_path)
            monkeypatch.setattr(ledger_module, "SESSION_TREE_READER", proctree.process_table)

            roots, tree, _ = proctree.session_tree(session[:8])
            # The venv's python.exe is a launcher that starts the real interpreter with
            # the same command line, so the stand-in is two processes -- as a real session
            # is a pty host and claude.exe.
            assert root.pid in [row.pid for row in roots]
            assert len(tree) >= 2

            def stop(*args: str) -> Any:
                # What `claude stop` does: the session's own processes, nothing below them.
                for row in roots:
                    proctree.terminate(row)
                root.wait(timeout=10)
                return _Completed()

            monkeypatch.setattr(ledger, "_session", stop)

            result = ledger._stop_session(a_session_record(tmp_path, session[:8]))

            assert result.stopped, result.detail
            after = {row.pid: row for row in proctree.process_table()}
            survivors = [row for row in tree if proctree._alive_in(list(after.values()), row)]
            assert survivors == [], result.detail
            assert "ended" in result.detail
        finally:
            if root.poll() is None:
                root.kill()


# ----- reading memory ----------------------------------------------------------------


def state(available: int, commit: int = 10_000, limit: int = 90_000) -> MemoryState:
    return MemoryState(
        available_mb=available, total_mb=64_000, commit_mb=commit, commit_limit_mb=limit
    )


class TestTheFloor:
    def test_low_on_physical_memory_or_on_commit_headroom(self) -> None:
        assert state(4_000).low(8_192)
        assert state(30_000, commit=85_000, limit=90_000).low(8_192)
        assert not state(30_000).low(8_192)

    def test_the_floor_comes_from_the_environment_when_it_is_a_number(self) -> None:
        assert memory.floor_mb({}) == memory.DEFAULT_FLOOR_MB
        assert memory.floor_mb({memory.FLOOR_ENV: "12000"}) == 12000
        assert memory.floor_mb({memory.FLOOR_ENV: "lots"}) == memory.DEFAULT_FLOOR_MB

    def test_this_machine_can_be_read(self) -> None:
        reading = memory.read_memory()
        assert reading is not None and reading.total_mb > 0


class TestTheSampler:
    def test_each_window_keeps_its_own_lowest(self) -> None:
        readings = iter([state(30_000), state(20_000), state(25_000), state(10_000)])
        sampler = memory.Sampler(reader=lambda: next(readings))
        sampler.open("pytest")  # 30
        sampler.read()  # 20
        sampler.open("vitest")  # 25
        now, lowest = sampler.close("pytest")  # 10
        assert now is not None and now.available_mb == 10_000
        assert lowest is not None and lowest.available_mb == 10_000
        sampler.stop()


class TestTheCensus:
    def table(self) -> List[Proc]:
        claude = proc(11, 1, 200, "claude.exe", f"claude.exe --session-id {SESSION}")
        return [
            proc(1, 0, 1, "wininit.exe"),
            claude,
            Proc(21, 11, 300, "python.exe", "C:/projects/worktrees/agentjobs-548/x", 10, 2 << 30),
            Proc(
                40,
                777,
                300,
                "python.exe",
                "pytest in C:/projects/worktrees/agentjobs-501",
                1,
                3 << 30,
            ),
            Proc(50, 1, 300, "chrome.exe", "", 1, 1 << 30),
        ]

    def test_rows_carry_parent_liveness_and_the_run_they_belong_to(self, tmp_path: Path) -> None:
        census = memory.take_census(
            tmp_path,
            table=self.table,
            reader=lambda: state(5_000),
            counters=lambda: {},
            sessions={"deadbeef": "run_abc"},
        )
        rows = {row.pid: row for row in census.rows}
        assert 50 not in rows  # not an agent image
        assert rows[21].run_id == "run_abc" and rows[21].parent_alive
        assert rows[40].worktree == "agentjobs-501" and not rows[40].parent_alive
        summary = census.summary()
        assert "1 with a dead parent (3.0 GB)" in summary
        assert "run_abc" in summary

    def test_it_fires_below_the_floor_and_at_most_once_per_interval(self, tmp_path: Path) -> None:
        kwargs: dict[str, Any] = dict(
            table=self.table, reader=lambda: state(5_000), counters=lambda: {}, sessions={}
        )
        env = {memory.FLOOR_ENV: "8192"}
        assert (
            memory.capture_if_low(tmp_path, state=state(20_000), reason="t", environ=env, **kwargs)
            is None
        )
        first = memory.capture_if_low(
            tmp_path, state=state(5_000), reason="t", environ=env, **kwargs
        )
        assert first is not None and (first / "census.json").is_file()
        payload = json.loads((first / "census.json").read_text(encoding="utf-8"))
        assert payload["reason"].startswith("t: 4.9 GB available")
        assert "RAMMap" in payload["rammap"]
        again = memory.capture_if_low(
            tmp_path, state=state(5_000), reason="t", environ=env, **kwargs
        )
        assert again is None
        later = memory.capture_if_low(
            tmp_path,
            state=state(5_000),
            reason="t",
            environ=env,
            now=time.time() + memory.AUTO_CAPTURE_INTERVAL_SECONDS + 5,
            **kwargs,
        )
        assert later is not None

    def test_the_watch_switch_turns_it_off(self, tmp_path: Path) -> None:
        env = {memory.WATCH_ENV: "off"}
        assert memory.capture_if_low(tmp_path, state=state(1), reason="t", environ=env) is None


# ----- the history and the finisher -------------------------------------------------


class TestHistoryCarriesMemory:
    def test_the_assembled_rows_hold_the_readings(self) -> None:
        assembler = GateAssembler("g1", origin="manual")
        assembler.feed("2026-09-23T00:00:00Z", "gate_started", {"free_mb": 30_000})
        assembler.feed(
            "2026-09-23T00:00:01Z", "gate_stage_started", {"stage": "pytest", "free_mb": 29_000}
        )
        assembler.feed(
            "2026-09-23T00:03:00Z",
            "gate_stage_finished",
            {
                "stage": "pytest",
                "seconds": 180,
                "free_mb": 28_000,
                "free_mb_low": 4_000,
                "low_memory": True,
            },
        )
        assembler.feed(
            "2026-09-23T00:03:01Z",
            "gate_finished",
            {"passed": True, "seconds": 181, "free_mb_low": 4_000, "low_memory": ["pytest"]},
        )
        assert assembler.record["free_mb_start"] == 30_000
        assert assembler.record["free_mb_low"] == 4_000 and assembler.record["low_memory"] is True
        stage = assembler.stages[0]
        assert (stage["free_mb_start"], stage["free_mb_end"], stage["free_mb_low"]) == (
            29_000,
            28_000,
            4_000,
        )
        assert stage["low_memory"] is True

    def test_the_store_keeps_them(self, tmp_path: Path) -> None:
        from agentjobs.api.models import GateHistoryWrite
        from agentjobs.sqlstore.connection import Database
        from agentjobs.sqlstore.history import upsert_gate_run
        from agentjobs.sqlstore import migrations

        database = Database(tmp_path / "db.sqlite3")
        migrations.upgrade(database, agentjobs_version="test", snapshot_before=False)
        body = GateHistoryWrite.model_validate(
            {
                "record": {
                    "origin": "manual",
                    "scope": "full",
                    "started_at": "2026-09-23T00:00:00Z",
                    "free_mb_start": 30_000,
                    "free_mb_low": 4_000,
                    "low_memory": True,
                },
                "stages": [
                    {
                        "seq": 1,
                        "stage": "pytest",
                        "started_at": "2026-09-23T00:00:01Z",
                        "free_mb_start": 29_000,
                        "free_mb_end": 28_000,
                        "free_mb_low": 4_000,
                        "low_memory": True,
                    }
                ],
            }
        )
        written = upsert_gate_run(
            database,
            "p",
            "g1",
            body.record.model_dump(),
            [stage.model_dump() for stage in body.stages],
        )
        assert written.written
        row = database.writer.execute(
            "SELECT free_mb_start, free_mb_low, low_memory FROM gate_run"
        ).fetchone()
        assert tuple(row) == (30_000, 4_000, 1)
        stage = database.writer.execute(
            "SELECT free_mb_start, free_mb_end, free_mb_low, low_memory FROM gate_stage"
        ).fetchone()
        assert tuple(stage) == (29_000, 28_000, 4_000, 1)
        database.close()


OUTPUT_LOW = (
    "Memory at start: 30.0 GB available\n"
    "> pytest\n"
    "FAILED tests/test_x.py::test_y - AssertionError: boom\n"
    "\nLOW MEMORY: `pytest` took 1200.0s and ran with as little as 3.9 GB available "
    "(floor 8.0 GB). Low memory is the likely cause if that is slower than usual.\n"
    "Failed at stage 'pytest'.\n"
)


class TestTheFinisherSaysSo:
    def test_the_gate_s_sentences_are_extracted(self) -> None:
        assert gate_memory(OUTPUT_LOW) == [
            "LOW MEMORY: `pytest` took 1200.0s and ran with as little as 3.9 GB available "
            "(floor 8.0 GB). Low memory is the likely cause if that is slower than usual."
        ]
        assert gate_memory("all fine") == []

    def test_a_red_gate_leads_with_the_memory(self, tmp_path: Path) -> None:
        text = lead_with_the_cause(OUTPUT_LOW, log=tmp_path / "gate.log")
        assert text.startswith("**The machine was short of memory when this ran:** LOW MEMORY")
        assert "tests/test_x.py::test_y" in text

    def test_a_green_gate_s_sentence_carries_it(self) -> None:
        attempt = GateAttempt(number=1, selection=[], head="h", base="b", corpus=None, ok=True)
        attempt.memory = gate_memory(OUTPUT_LOW)
        sentence = GateVerdict([attempt]).sentence()
        assert sentence.startswith("`scripts/check.py` ran green")
        assert "**The machine was short of memory:** LOW MEMORY" in sentence
        attempt.memory = []
        assert GateVerdict([attempt]).sentence() == "`scripts/check.py` ran green"


# ----- the gate itself ---------------------------------------------------------------


def load_check() -> Any:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("check_task548", root / "scripts" / "check.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(root / "scripts"))
    sys.modules["check_task548"] = module  # a dataclass resolves its module by name
    spec.loader.exec_module(module)
    return module


class TestTheGateNamesLowMemory:
    def gate_memory(self, readings: List[int], floor: str = "8192") -> Any:
        check = load_check()
        values = iter(readings)
        last: dict[str, Optional[MemoryState]] = {"v": None}

        class Fake:
            FLOOR_ENV = memory.FLOOR_ENV
            gb = staticmethod(memory.gb)

            @staticmethod
            def floor_mb() -> int:
                return int(floor)

            @staticmethod
            def Sampler() -> memory.Sampler:
                def reader() -> Optional[MemoryState]:
                    try:
                        last["v"] = state(next(values))
                    except StopIteration:
                        pass
                    return last["v"]

                return memory.Sampler(reader=reader, interval=3600)

            @staticmethod
            def capture_if_low(home: Path, **kwargs: Any) -> Path:
                return home / "census-here"

        return check, check.GateMemory(Fake, Path("C:/home"))

    def test_a_stage_that_dips_below_the_floor_is_named_as_the_likely_cause(self) -> None:
        check, watch = self.gate_memory([30_000, 29_000, 3_000, 28_000])
        assert "Memory at start: 29.3 GB available" in watch.opening()
        watch.begin("pytest")  # 29
        watch.sampler.read()  # 3, mid-stage
        record = watch.end("pytest")  # 28
        assert record.low and record.low_mb == 3_000
        notes = watch.notes([("pytest", 1200.0)])
        assert notes[0].startswith(
            "LOW MEMORY: `pytest` took 1200.0s and ran with as little as 2.9 GB"
        )
        assert "census" in notes[-1]
        table = watch.table([("pytest", 1200.0)])
        assert "pytest" in table and table.rstrip().endswith("LOW")

    def test_a_healthy_machine_says_nothing_alarming(self) -> None:
        check, watch = self.gate_memory([30_000, 29_000, 28_000, 28_000])
        watch.begin("pytest")
        watch.end("pytest")
        assert watch.notes([("pytest", 180.0)]) == []
        assert "LOW" not in watch.table([("pytest", 180.0)])
        assert not watch.started_low

    def test_a_gate_that_starts_low_says_so_up_front(self) -> None:
        check, watch = self.gate_memory([2_000])
        opening = watch.opening()
        assert "LOW MEMORY: this gate started with 2.0 GB available" in opening
