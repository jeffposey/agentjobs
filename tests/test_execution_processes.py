"""The execution journal across real processes (task-264, durable-1, durable-2, durable-6).

Threads share one connection and one Python lock, so a threaded race proves the lock and
not the database. Every test here starts separate interpreters against one journal file,
which is the concurrency the journal exists for: the server, a CLI dispatch and a
finish's escalation are three processes on a normal day.

Children are plain ``python -c`` scripts rather than ``multiprocessing`` workers, so each
is a genuinely fresh process with nothing inherited but the file path, and they are
released together by a go-file so the contention is real rather than sequential.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, TypeVar

from agentjobs.execution.coordinator import advance_execution, inspect_execution
from agentjobs.execution.reducer import WORKFLOW_VERSION
from agentjobs.execution.store import ExecutionStore

CHILD_PREAMBLE = """
import json, pathlib, sys, time
from agentjobs.execution.store import ExecutionStore
from agentjobs.execution.errors import CapacityExhausted, OwnershipConflict, ExecutionStoreError
db, go = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
store = ExecutionStore(db, busy_timeout_ms=20000)
deadline = time.monotonic() + 60
while not go.exists():
    if time.monotonic() > deadline:
        raise SystemExit("never released")
    time.sleep(0.001)
"""

ADMIT_CHILD = (
    CHILD_PREAMBLE
    + """
project, task, run = sys.argv[3], sys.argv[4], sys.argv[5]
try:
    store.admit(project_id=project, task_id=task, run_id=run, capacity=4,
                envelope={"runner": "x"}, workflow_version=1)
    print(json.dumps({"run": run, "result": "admitted"}))
except CapacityExhausted:
    print(json.dumps({"run": run, "result": "capacity"}))
except OwnershipConflict:
    print(json.dumps({"run": run, "result": "owned"}))
"""
)

CONCLUDE_CHILD = (
    CHILD_PREAMBLE
    + """
outcome = sys.argv[3]
conclusion = store.conclude("run_shared", outcome=outcome, status="finished", concluded_by=outcome)
print(json.dumps({"outcome": outcome, "won": conclusion.won, "final": conclusion.attempt.outcome}))
"""
)

REPLAY_CHILD = """
import json, pathlib, sys
from agentjobs.execution.store import ExecutionStore
from agentjobs.execution.coordinator import advance_execution
from agentjobs.execution.errors import HistoryIncompatible
store = ExecutionStore(pathlib.Path(sys.argv[1]))
before = len(store.activities())
try:
    result = advance_execution(store, sys.argv[2])
except HistoryIncompatible as exc:
    print(json.dumps({"refused": str(exc), "activities": len(store.activities()), "before": before}))
    raise SystemExit(0)
print(json.dumps({
    "state": result.state.as_data(),
    "intents": [[i.kind, i.activity_id, dict(i.input)] for i in result.intents],
    "recorded": list(result.recorded),
    "activities": len(store.activities()),
    "before": before,
}))
"""

DIE_MID_COMMIT_CHILD = """
import os, pathlib, sys
from agentjobs.execution.store import ExecutionStore
store = ExecutionStore(pathlib.Path(sys.argv[1]))
store.before_commit = lambda label: os._exit(9)
store.conclude("run_a", outcome="completed", status="finished", concluded_by="doomed")
"""

_T = TypeVar("_T")


def must(value: Optional[_T]) -> _T:
    """The value, asserted present -- a lookup the test has just made true."""
    assert value is not None
    return value


def environment() -> dict:
    env = dict(os.environ)
    source = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = source + os.pathsep + env.get("PYTHONPATH", "")
    return env


def race(script: str, db: Path, go: Path, argument_sets: List[List[str]]) -> List[dict]:
    children = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(db), str(go), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment(),
        )
        for arguments in argument_sets
    ]
    time.sleep(1.5)  # every interpreter imported and waiting on the go-file
    go.write_text("go", encoding="utf-8")
    outputs = []
    for child in children:
        stdout, stderr = child.communicate(timeout=120)
        assert child.returncode == 0, stderr
        outputs.append(json.loads(stdout.strip().splitlines()[-1]))
    return outputs


class TestAdmissionAcrossProcesses:
    def test_two_projects_at_capacity_minus_one_award_exactly_one_slot(
        self, tmp_path: Path
    ) -> None:
        """durable-2, first half: eight processes, three held slots, one free."""
        db = tmp_path / "execution.db"
        store = ExecutionStore(db)
        for index, project in enumerate(("alpha", "beta", "gamma")):
            store.admit(
                project_id=project,
                task_id=f"task-10{index}",
                run_id=f"run_held_{index}",
                capacity=4,
                envelope={},
                workflow_version=WORKFLOW_VERSION,
            )
        store.close()

        contenders = [
            [project, f"task-00{index}", f"run_{project}_{index}"]
            for index in range(4)
            for project in ("alpha", "beta")
        ]
        outcomes = race(ADMIT_CHILD, db, tmp_path / "go", contenders)

        admitted = [o for o in outcomes if o["result"] == "admitted"]
        assert len(admitted) == 1, outcomes
        assert {o["result"] for o in outcomes} == {"admitted", "capacity"}
        check = ExecutionStore(db)
        try:
            assert len([a for a in check.live_attempts() if a.takes_slot]) == 4
        finally:
            check.close()

    def test_identical_task_ids_in_different_projects_never_share_ownership(
        self, tmp_path: Path
    ) -> None:
        """durable-2, second half: each project's task-001 has exactly one owner."""
        db = tmp_path / "execution.db"
        ExecutionStore(db).close()
        contenders = [
            [project, "task-001", f"run_{project}_{index}"]
            for index in range(3)
            for project in ("alpha", "beta")
        ]
        outcomes = race(ADMIT_CHILD, db, tmp_path / "go", contenders)

        winners = {o["run"].split("_")[1] for o in outcomes if o["result"] == "admitted"}
        assert winners == {"alpha", "beta"}, outcomes
        assert sum(o["result"] == "admitted" for o in outcomes) == 2
        assert {o["result"] for o in outcomes if o["result"] != "admitted"} == {"owned"}


class TestTheTerminalTransitionAcrossProcesses:
    def test_many_concluders_one_winner_and_everyone_reads_its_outcome(
        self, tmp_path: Path
    ) -> None:
        db = tmp_path / "execution.db"
        store = ExecutionStore(db)
        store.admit(project_id="alpha", task_id="task-001", run_id="run_shared", capacity=3)
        store.close()
        outcomes = race(
            CONCLUDE_CHILD,
            db,
            tmp_path / "go",
            [[outcome] for outcome in ("cancelled", "interrupted", "completed", "failed") * 2],
        )
        winners = [o for o in outcomes if o["won"]]
        assert len(winners) == 1, outcomes
        assert {o["final"] for o in outcomes} == {winners[0]["outcome"]}

    def test_a_process_killed_inside_its_commit_leaves_nothing_and_frees_nothing(
        self, tmp_path: Path
    ) -> None:
        db = tmp_path / "execution.db"
        store = ExecutionStore(db)
        store.admit(project_id="alpha", task_id="task-001", run_id="run_a", capacity=3)
        store.close()
        doomed = subprocess.run(
            [sys.executable, "-c", DIE_MID_COMMIT_CHILD, str(db)],
            capture_output=True,
            text=True,
            env=environment(),
            timeout=120,
        )
        assert doomed.returncode == 9, doomed.stderr
        survivor = ExecutionStore(db)
        try:
            attempt = must(survivor.attempt("run_a"))
            assert attempt.is_live and attempt.outcome is None
            assert survivor.conclude(
                "run_a", outcome="completed", status="finished", concluded_by="next process"
            ).won
        finally:
            survivor.close()


class TestReplayInAFreshProcess:
    def seed(self, db: Path) -> str:
        store = ExecutionStore(db)
        store.admit(
            project_id="alpha",
            task_id="task-001",
            run_id="run_a",
            capacity=3,
            envelope={"runner": "claude-opus-5", "merge_mode": "review"},
            workflow_version=WORKFLOW_VERSION,
        )
        store.mark_launched("run_a", session_id="s-1")
        execution = must(store.open_execution("alpha", "task-001"))
        store.close()
        return execution.execution_id

    def replay_in_child(self, db: Path, execution_id: str) -> dict:
        completed = subprocess.run(
            [sys.executable, "-c", REPLAY_CHILD, str(db), execution_id],
            capture_output=True,
            text=True,
            env=environment(),
            timeout=120,
        )
        assert completed.returncode == 0, completed.stderr
        answer: dict = json.loads(completed.stdout.strip().splitlines()[-1])
        return answer

    def test_identical_state_and_intents_and_no_effect_from_replaying(self, tmp_path: Path) -> None:
        """durable-1: two fresh processes, one history, one answer."""
        db = tmp_path / "execution.db"
        execution_id = self.seed(db)
        reader = ExecutionStore(db)
        try:
            expected_state, expected_intents = inspect_execution(reader, execution_id)
        finally:
            reader.close()

        first = self.replay_in_child(db, execution_id)
        second = self.replay_in_child(db, execution_id)

        assert first["state"] == expected_state.as_data()
        assert second["state"] == first["state"]
        assert first["intents"] == [
            [i.kind, i.activity_id, dict(i.input)] for i in expected_intents
        ]
        assert second["intents"] == first["intents"]
        assert [kind for kind, _, _ in first["intents"]] == ["observe"]
        assert first["recorded"] and second["recorded"] == [], "the second replay records nothing"
        assert second["activities"] == first["activities"]
        check = ExecutionStore(db)
        try:
            assert all(activity.shadow for activity in check.activities())
            assert must(check.attempt("run_a")).is_live, "replay concluded nothing"
            assert not list(tmp_path.glob("runs/*")), "and launched nothing"
        finally:
            check.close()

    def test_an_incompatible_history_refuses_the_mutation(self, tmp_path: Path) -> None:
        db = tmp_path / "execution.db"
        execution_id = self.seed(db)
        raw = sqlite3.connect(str(db))
        raw.execute(
            "UPDATE execution SET workflow_version = ? WHERE execution_id = ?",
            (WORKFLOW_VERSION + 1, execution_id),
        )
        raw.commit()
        raw.close()

        answer = self.replay_in_child(db, execution_id)
        assert "workflow version" in answer["refused"]
        assert answer["activities"] == answer["before"] == 0

    def test_replay_from_a_snapshot_equals_replay_from_the_beginning(self, tmp_path: Path) -> None:
        db = tmp_path / "execution.db"
        execution_id = self.seed(db)
        store = ExecutionStore(db)
        try:
            advance_execution(store, execution_id)  # writes a snapshot
            store.request_cancel("run_a", requester="Jeff Posey", source="gui")
            from_snapshot = advance_execution(store, execution_id)
            from_scratch, intents = inspect_execution(store, execution_id)
            assert from_snapshot.state == from_scratch
            assert from_snapshot.intents == intents
            assert [intent.kind for intent in intents] == ["stop"]
        finally:
            store.close()
